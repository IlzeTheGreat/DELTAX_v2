from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"

TRADING_BASE_DEFAULT = "https://paper-api.alpaca.markets/v2"
REQUEST_TIMEOUT = 20

BASKET = [
    {"symbol": "IWM", "side": "sell", "direction": "SHORT"},
    {"symbol": "QQQ", "side": "sell", "direction": "SHORT"},
    {"symbol": "SPY", "side": "sell", "direction": "SHORT"},
    {"symbol": "XLE", "side": "buy",  "direction": "LONG"},
]


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

    def _get(self, path: str):
        r = requests.get(
            f"{self.cfg['trading_url']}{path}",
            headers=self.headers,
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            raise RuntimeError(
                f"GET {path} failed: {r.status_code} {r.text[:500]}"
            )
        return r.json()

    def _post(self, path: str, payload: dict):
        r = requests.post(
            f"{self.cfg['trading_url']}{path}",
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            raise RuntimeError(
                f"POST {path} failed: {r.status_code} {r.text[:500]}"
            )
        return r.json()

    def account(self):
        return self._get("/account")

    def clock(self):
        return self._get("/clock")

    def asset(self, symbol: str):
        return self._get(f"/assets/{symbol}")

    def submit_market_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        client_order_id: str,
    ):
        return self._post(
            "/orders",
            {
                "symbol": symbol,
                "qty": str(qty),
                "side": side,
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id,
            },
        )


def build_client_order_id(symbol: str, side: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"dxe-{stamp}-{symbol.lower()}-{side}"[:48]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Trade EVENT paper basket: SHORT IWM/QQQ/SPY and LONG XLE."
        )
    )
    parser.add_argument(
        "--qty",
        type=int,
        default=1,
        help="Integer shares per symbol. Default: 1.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit paper orders. Without this flag, dry-run only.",
    )
    args = parser.parse_args()

    if args.qty < 1:
        raise ValueError("--qty must be >= 1")

    cfg = load_environment()
    alpaca = AlpacaEventClient(cfg)

    account = alpaca.account()
    clock = alpaca.clock()

    print("=" * 72)
    print("DELTAX EVENT BASKET TRADE")
    print("=" * 72)
    print(f"Account:      {account.get('account_number', 'n/a')}")
    print(f"Equity:       {account.get('equity')}")
    print(f"Buying power: {account.get('buying_power')}")
    print(f"Market open:  {clock.get('is_open')}")
    print(f"Mode:         {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"Qty/symbol:   {args.qty}")
    print()

    results = []

    for item in BASKET:
        symbol = item["symbol"]
        side = item["side"]
        direction = item["direction"]

        try:
            asset = alpaca.asset(symbol)

            if not asset.get("tradable", False):
                raise RuntimeError(f"{symbol} is not tradable")

            if side == "sell" and not asset.get("shortable", False):
                raise RuntimeError(f"{symbol} is not shortable")

            result = {
                "symbol": symbol,
                "direction": direction,
                "side": side,
                "qty": args.qty,
                "tradable": asset.get("tradable"),
                "shortable": asset.get("shortable"),
                "status": "DRY_RUN",
            }

            if args.execute:
                client_order_id = build_client_order_id(symbol, side)
                order = alpaca.submit_market_order(
                    symbol=symbol,
                    qty=args.qty,
                    side=side,
                    client_order_id=client_order_id,
                )
                result.update(
                    {
                        "status": order.get("status"),
                        "order_id": order.get("id"),
                        "client_order_id": order.get("client_order_id"),
                    }
                )

            results.append(result)
            print(json.dumps(result, indent=2))

        except Exception as exc:
            result = {
                "symbol": symbol,
                "direction": direction,
                "side": side,
                "qty": args.qty,
                "status": "ERROR",
                "error": str(exc),
            }
            results.append(result)
            print(json.dumps(result, indent=2))

    failed = [r for r in results if r["status"] == "ERROR"]

    print()
    print("=" * 72)
    print(f"Completed: {len(results) - len(failed)}")
    print(f"Errors:    {len(failed)}")
    print("=" * 72)

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
