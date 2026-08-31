from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, time as dt_time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
ENV_PATH = ROOT_DIR / ".env"
STATE_FILE = SCRIPT_DIR / "deltax_event_iran_v2_state.json"

NY = ZoneInfo("America/New_York")
EXIT_TIME = dt_time(15, 50)

DEFAULT_TRADING_URL = "https://paper-api.alpaca.markets/v2"


def load_config():
    load_dotenv(ENV_PATH)

    key = (os.getenv("ALPACA_API_KEY_EVENT") or "").strip()
    secret = (os.getenv("ALPACA_API_SECRET_EVENT") or "").strip()
    trading_url = (
        os.getenv("ALPACA_TRADING_URL_EVENT")
        or DEFAULT_TRADING_URL
    ).strip().rstrip("/")

    if not trading_url.endswith("/v2"):
        trading_url += "/v2"

    if not key or not secret:
        raise RuntimeError("Missing EVENT Alpaca credentials in .env")

    return trading_url, {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def api_get(base, headers, path):
    r = requests.get(f"{base}{path}", headers=headers, timeout=15)
    if not r.ok:
        raise RuntimeError(f"GET {path}: {r.status_code} {r.text[:500]}")
    return r.json()


def api_delete(base, headers, path):
    r = requests.delete(f"{base}{path}", headers=headers, timeout=15)

    # Alpaca can return success responses with different 2xx codes.
    if r.status_code < 200 or r.status_code >= 300:
        raise RuntimeError(f"DELETE {path}: {r.status_code} {r.text[:500]}")

    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code}


def load_state():
    if not STATE_FILE.exists():
        return {"days": {}}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read state file: {exc}") from exc


def save_state(state):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


def tradable_symbol(underlying, order):
    option = order.get("option") or {}

    return (
        order.get("tradable_symbol")
        or order.get("option_symbol")
        or option.get("symbol")
        or order.get("symbol")
        or underlying
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually close adaptive paper positions after 15:50 ET.",
    )
    args = parser.parse_args()

    base, headers = load_config()

    clock = api_get(base, headers, "/clock")
    now = datetime.fromisoformat(
        clock["timestamp"].replace("Z", "+00:00")
    ).astimezone(NY)

    print()
    print("=" * 92)
    print("DELTAX ADAPTIVE POSITION MANAGER")
    print("=" * 92)
    print(f"Time ET:       {now.isoformat()}")
    print(f"Market open:   {clock.get('is_open')}")
    print(f"Exit time:     15:50 ET")
    print(f"Execute mode:  {args.execute}")

    trading_day = now.date().isoformat()
    state = load_state()
    sday = state.setdefault("days", {}).setdefault(trading_day, {})

    adaptive_orders = sday.get("adaptive_orders", {})
    adaptive_exits = sday.setdefault("adaptive_exits", {})

    if not adaptive_orders:
        print("No adaptive orders recorded for today.")
        return 0

    print(f"Adaptive orders: {', '.join(sorted(adaptive_orders))}")

    if now.time() < EXIT_TIME:
        print("Exit time not reached. Positions remain open.")
        return 0

    if not clock.get("is_open"):
        print("Market is closed. Cannot send normal closing orders.")
        return 0

    positions = api_get(base, headers, "/positions")
    open_symbols = {str(p.get("symbol")): p for p in positions}

    for underlying, order in adaptive_orders.items():
        if underlying in adaptive_exits:
            print(f"{underlying}: already processed for exit.")
            continue

        symbol = tradable_symbol(underlying, order)

        if symbol not in open_symbols:
            # Stock positions use the underlying symbol. This also handles
            # the case where the position has already been manually closed.
            if underlying in open_symbols:
                symbol = underlying
            else:
                print(f"{underlying}: no matching open position; marking as already closed.")
                adaptive_exits[underlying] = {
                    "status": "not_open",
                    "symbol": symbol,
                    "checked_at": now.isoformat(),
                }
                save_state(state)
                continue

        if not args.execute:
            print(f"{underlying}: DRY RUN would close position {symbol}.")
            continue

        try:
            result = api_delete(
                base,
                headers,
                f"/positions/{quote(symbol, safe='')}",
            )

            adaptive_exits[underlying] = {
                "status": "close_submitted",
                "symbol": symbol,
                "submitted_at": now.isoformat(),
                "response": result,
            }
            save_state(state)

            print(f"{underlying}: CLOSE SUBMITTED for {symbol}")

        except Exception as exc:
            print(f"{underlying}: EXIT ERROR: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
