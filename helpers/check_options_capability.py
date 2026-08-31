# File: helpers/check_options_capability.py
# Purpose: Read-only DELTAX options readiness check.
# Verifies Alpaca paper-account permissions, option market-data access,
# and DB readiness. Performs no writes and submits no orders.

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]

DEFAULT_SYMBOL = "AAPL"
MIN_DTE = 7
MAX_DTE = 21


def json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def response_data(response):
    return response.data if hasattr(response, "data") else response


def compact_snapshot(snapshot):
    quote = getattr(snapshot, "latest_quote", None)
    greeks = getattr(snapshot, "greeks", None)

    return {
        "bid": getattr(quote, "bid_price", None) if quote else None,
        "ask": getattr(quote, "ask_price", None) if quote else None,
        "implied_volatility": getattr(snapshot, "implied_volatility", None),
        "delta": getattr(greeks, "delta", None) if greeks else None,
        "gamma": getattr(greeks, "gamma", None) if greeks else None,
        "theta": getattr(greeks, "theta", None) if greeks else None,
        "vega": getattr(greeks, "vega", None) if greeks else None,
    }


def db_check():
    required_tables = {
        "trade_theses",
        "trade_intents",
        "trade_intent_legs",
        "option_quote_snapshots",
        "positions",
        "position_legs",
        "portfolio_snapshots",
        "earnings_events",
        "bot_control",
        "risk_events",
    }

    with psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
                """,
                (list(required_tables),),
            )
            present = {row["table_name"] for row in cursor.fetchall()}

            cursor.execute(
                """
                SELECT
                    trading_mode,
                    execution_enabled,
                    new_entries_enabled,
                    kill_switch_active
                FROM bot_control
                WHERE id = 1
                """
            )
            control = cursor.fetchone()

            cursor.execute(
                """
                SELECT
                    COUNT(DISTINCT trade_theses.id) FILTER (
                        WHERE trade_theses.status = 'approved'
                          AND trade_theses.strategy IN ('core', 'active')
                          AND trade_theses.expires_at > now()
                    ) AS approved_core_active_theses,
                    COUNT(DISTINCT trade_intents.id) FILTER (
                        WHERE trade_intents.asset_class = 'option_spread'
                          AND trade_intents.intent_type = 'entry'
                    ) AS option_entry_intents
                FROM trade_theses
                LEFT JOIN trade_intents
                  ON trade_intents.trade_thesis_id = trade_theses.id
                """
            )
            counts = cursor.fetchone()

    return {
        "required_tables_present": sorted(present),
        "missing_tables": sorted(required_tables - present),
        "bot_control": dict(control) if control else None,
        "counts": dict(counts) if counts else {},
    }


def alpaca_account_check():
    trading = TradingClient(
        ALPACA_API_KEY,
        ALPACA_API_SECRET,
        paper=True,
    )
    account = trading.get_account()
    clock = trading.get_clock()

    return {
        "paper_client": True,
        "account_status": str(getattr(account, "status", "")),
        "equity": getattr(account, "equity", None),
        "buying_power": getattr(account, "buying_power", None),
        "options_buying_power": getattr(account, "options_buying_power", None),
        "options_approved_level": getattr(account, "options_approved_level", None),
        "options_trading_level": getattr(account, "options_trading_level", None),
        "trading_blocked": bool(getattr(account, "trading_blocked", False)),
        "account_blocked": bool(getattr(account, "account_blocked", False)),
        "market_open": bool(clock.is_open),
        "clock_timestamp": clock.timestamp,
    }


def option_data_check(symbol):
    client = OptionHistoricalDataClient(
        ALPACA_API_KEY,
        ALPACA_API_SECRET,
    )

    today = datetime.now(timezone.utc).date()
    request = OptionChainRequest(
        underlying_symbol=symbol,
        expiration_date_gte=today + timedelta(days=MIN_DTE),
        expiration_date_lte=today + timedelta(days=MAX_DTE),
    )

    chain = response_data(client.get_option_chain(request))
    items = list(chain.items()) if hasattr(chain, "items") else []

    sample = []
    with_quotes = 0
    with_greeks = 0
    with_iv = 0

    for contract_symbol, snapshot in items:
        quote = getattr(snapshot, "latest_quote", None)
        greeks = getattr(snapshot, "greeks", None)
        iv = getattr(snapshot, "implied_volatility", None)

        if (
            quote is not None
            and getattr(quote, "bid_price", None) is not None
            and getattr(quote, "ask_price", None) is not None
        ):
            with_quotes += 1

        if greeks is not None and getattr(greeks, "delta", None) is not None:
            with_greeks += 1

        if iv is not None:
            with_iv += 1

        if len(sample) < 5:
            sample.append(
                {
                    "contract_symbol": contract_symbol,
                    **compact_snapshot(snapshot),
                }
            )

    return {
        "underlying_symbol": symbol,
        "dte_window": [MIN_DTE, MAX_DTE],
        "contracts_returned": len(items),
        "contracts_with_bid_ask": with_quotes,
        "contracts_with_delta": with_greeks,
        "contracts_with_iv": with_iv,
        "sample": sample,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Read-only DELTAX Alpaca options capability check."
    )
    parser.add_argument(
        "--symbol",
        default=DEFAULT_SYMBOL,
        help="Underlying symbol used for the option-chain data test.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    symbol = args.symbol.strip().upper()

    if not symbol:
        raise ValueError("--symbol must not be empty")

    result = {
        "database": db_check(),
        "alpaca_account": alpaca_account_check(),
        "option_market_data": option_data_check(symbol),
        "database_writes_performed": False,
        "broker_orders_submitted": False,
    }

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=json_default,
        )
    )
    print("OPTIONS CAPABILITY CHECK: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
