from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"

TRADING_BASE_DEFAULT = "https://paper-api.alpaca.markets/v2"
DATA_BASE = "https://data.alpaca.markets"
REQUEST_TIMEOUT = 20


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

    if not trading_url.endswith("/v2"):
        trading_url += "/v2"

    return {
        "key": key,
        "secret": secret,
        "trading_url": trading_url,
        "feed": feed,
    }


class AlpacaEventClient:
    def __init__(self, cfg: dict[str, str]):
        self.cfg = cfg
        self.headers = {
            "APCA-API-KEY-ID": cfg["key"],
            "APCA-API-SECRET-KEY": cfg["secret"],
        }

    def _get(self, url: str, params: dict | None = None):
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

    def _post(self, url: str, payload: dict):
        r = requests.post(
            url,
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            raise RuntimeError(
                f"POST {url} failed: {r.status_code} {r.text[:500]}"
            )
        return r.json()

    def account(self):
        return self._get(f"{self.cfg['trading_url']}/account")

    def clock(self):
        return self._get(f"{self.cfg['trading_url']}/clock")

    def asset(self, symbol: str):
        return self._get(f"{self.cfg['trading_url']}/assets/{symbol}")

    def latest_trade_price(self, symbol: str) -> float:
        data = self._get(
            f"{DATA_BASE}/v2/stocks/{symbol}/trades/latest",
            params={"feed": self.cfg["feed"]},
        )
        trade = data.get("trade") or {}
        price = trade.get("p")
        if price is None:
            raise RuntimeError(f"No latest trade price returned for {symbol}")
        return float(price)

    def submit_market_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        client_order_id: str,
    ):
        return self._post(
            f"{self.cfg['trading_url']}/orders",
            {
                "symbol": symbol,
                "qty": str(qty),
                "side": side,
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            },
        )


def ask_ticker() -> str:
    while True:
        ticker = input("Ticker: ").strip().upper()
        if ticker and ticker.replace(".", "").replace("-", "").isalnum():
            return ticker
        print("Invalid ticker. Try again.")


def ask_amount() -> float:
    while True:
        raw = input("Amount to trade in USD: $").strip().replace(",", "")
        try:
            amount = float(raw)
        except ValueError:
            print("Enter a number, e.g. 1000")
            continue

        if amount > 0:
            return amount

        print("Amount must be greater than 0.")


def ask_direction() -> str:
    while True:
        value = input("Direction [long/short]: ").strip().lower()
        if value in {"long", "l"}:
            return "long"
        if value in {"short", "s"}:
            return "short"
        print("Enter long or short.")


def confirm() -> bool:
    return input("Submit PAPER order? [y/N]: ").strip().lower() in {"y", "yes"}


def client_order_id(symbol: str, direction: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"dxe-manual-{stamp}-{symbol.lower()}-{direction}"[:48]


def main() -> int:
    cfg = load_environment()
    alpaca = AlpacaEventClient(cfg)

    print("=" * 68)
    print("DELTAX EVENT MANUAL PAPER TRADER")
    print("=" * 68)
    print()

    ticker = ask_ticker()
    amount = ask_amount()
    direction = ask_direction()

    account = alpaca.account()
    clock = alpaca.clock()
    asset = alpaca.asset(ticker)

    if not asset.get("tradable", False):
        raise RuntimeError(f"{ticker} is not tradable on Alpaca")

    if direction == "short" and not asset.get("shortable", False):
        raise RuntimeError(f"{ticker} is not currently shortable on Alpaca")

    price = alpaca.latest_trade_price(ticker)
    qty = math.floor(amount / price)

    if qty < 1:
        raise RuntimeError(
            f"${amount:,.2f} is not enough for one whole share of "
            f"{ticker} at about ${price:,.2f}"
        )

    estimated_value = qty * price
    side = "buy" if direction == "long" else "sell"

    print()
    print("-" * 68)
    print(f"Account:          {account.get('account_number', 'n/a')}")
    print(f"Market open:      {clock.get('is_open')}")
    print(f"Ticker:           {ticker}")
    print(f"Direction:        {direction.upper()}")
    print(f"Latest price:     ${price:,.2f}")
    print(f"Requested amount: ${amount:,.2f}")
    print(f"Whole shares:     {qty}")
    print(f"Estimated value:  ${estimated_value:,.2f}")
    print(f"Order:            MARKET {side.upper()} {qty} {ticker}")
    print("-" * 68)
    print()

    if not confirm():
        print("Cancelled. No order submitted.")
        return 0

    order = alpaca.submit_market_order(
        symbol=ticker,
        qty=qty,
        side=side,
        client_order_id=client_order_id(ticker, direction),
    )

    print()
    print("ORDER SUBMITTED")
    print(json.dumps(
        {
            "id": order.get("id"),
            "client_order_id": order.get("client_order_id"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "qty": order.get("qty"),
            "type": order.get("type"),
            "status": order.get("status"),
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
