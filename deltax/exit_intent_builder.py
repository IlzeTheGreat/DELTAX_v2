# File: deltax/exit_intent_builder.py
# Purpose: Build deterministic STOCK and OPTION-SPREAD exit intents for open
# DELTAX positions. Does NOT submit broker orders.
#
# Exit rules implemented:
#
# STOCK
#   - stop loss
#   - take profit
#   - portfolio kill switch -> emergency_exit
#
# OPTION CREDIT SPREAD
#   - take profit when estimated close debit <= 50% of initial credit
#   - loss exit when estimated close debit >= 2x initial credit
#   - exit at <= 3 DTE
#   - exit before scheduled earnings on/within next calendar day
#   - portfolio kill switch -> emergency_exit
#
# Option close debit is estimated conservatively:
#   short leg -> buy at ask
#   long leg  -> sell at bid
#
# Safety:
#   --check is read-only
#   --process writes trade_intents + trade_intent_legs only
#   never submits/cancels broker orders

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.client import TradingClient


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]

OPTION_PROFIT_TARGET_FRACTION = Decimal("0.50")
OPTION_STOP_MULTIPLE = Decimal("2.0")
OPTION_DTE_EXIT = 3
EARNINGS_EXIT_DAYS = 1
INTENT_TTL_MINUTES = 4
MAX_PROCESS_LIMIT = 50


def D(value, default=None):
    if value is None:
        return default
    return Decimal(str(value))


def q2(value: Decimal) -> Decimal:
    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def model_payload(value):
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return {"repr": str(value)}


def occ_expiration(contract_symbol: str) -> date:
    # OCC equity option symbol:
    # AAPL260918C00200000
    match = re.search(
        r"(\d{6})[CP]\d{8}$",
        contract_symbol,
    )
    if not match:
        raise ValueError(
            f"Cannot parse OCC expiration: {contract_symbol}"
        )
    return datetime.strptime(
        match.group(1),
        "%y%m%d",
    ).date()


class ExitIntentBuilder:
    def __init__(self):
        self.trading = TradingClient(
            ALPACA_API_KEY,
            ALPACA_API_SECRET,
            paper=True,
        )
        self.option_data = OptionHistoricalDataClient(
            ALPACA_API_KEY,
            ALPACA_API_SECRET,
        )

    def validate_schema(self, cursor):
        required = {
            "positions": {
                "id",
                "trade_thesis_id",
                "entry_intent_id",
                "symbol",
                "asset_class",
                "strategy",
                "direction",
                "status",
                "quantity",
                "average_entry_price",
                "current_price",
                "stop_loss_price",
                "take_profit_price",
                "initial_max_loss",
                "opened_at",
            },
            "position_legs": {
                "position_id",
                "contract_symbol",
                "side",
                "quantity",
                "multiplier",
                "average_entry_price",
            },
            "trade_intents": {
                "id",
                "trade_thesis_id",
                "strategy_config_id",
                "intent_type",
                "asset_class",
                "strategy",
                "direction",
                "symbol",
                "side",
                "quantity",
                "order_type",
                "time_in_force",
                "limit_price",
                "premium_type",
                "net_premium",
                "max_profit",
                "max_loss",
                "idempotency_key",
                "status",
                "expires_at",
                "metadata",
                "position_id",
            },
            "trade_intent_legs": {
                "trade_intent_id",
                "leg_number",
                "contract_symbol",
                "action",
                "ratio_quantity",
                "option_type",
                "strike",
                "expiration_date",
                "multiplier",
                "reference_bid",
                "reference_ask",
                "reference_mid",
            },
            "bot_control": {
                "id",
                "trading_mode",
                "execution_enabled",
                "new_entries_enabled",
                "kill_switch_active",
                "kill_switch_reason",
            },
            "earnings_events": {
                "symbol",
                "report_date",
                "status",
            },
        }

        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
            """,
            (list(required),),
        )

        actual = {}
        for row in cursor.fetchall():
            actual.setdefault(
                row["table_name"],
                set(),
            ).add(row["column_name"])

        missing = {
            table: sorted(
                cols - actual.get(table, set())
            )
            for table, cols in required.items()
            if cols - actual.get(table, set())
        }

        if missing:
            raise RuntimeError(
                f"Required exit-builder schema missing: {missing}"
            )

    def load_control(self, cursor):
        cursor.execute(
            """
            SELECT *
            FROM bot_control
            WHERE id = 1
            """
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                "bot_control row id=1 missing"
            )
        return row

    def account_state(self):
        account = self.trading.get_account()
        clock = self.trading.get_clock()

        return {
            "paper_client": True,
            "account_status": str(
                getattr(account, "status", "")
            ),
            "account_blocked": bool(
                getattr(account, "account_blocked", False)
            ),
            "trading_blocked": bool(
                getattr(account, "trading_blocked", False)
            ),
            "market_open": bool(clock.is_open),
            "clock_timestamp": clock.timestamp,
        }

    def load_open_positions(self, cursor, limit):
        cursor.execute(
            """
            SELECT
                positions.*,
                entry_intent.strategy_config_id,
                entry_intent.limit_price
                    AS entry_limit_price,
                entry_intent.net_premium
                    AS entry_net_premium,
                entry_intent.max_profit
                    AS entry_max_profit
            FROM positions
            JOIN trade_intents entry_intent
              ON entry_intent.id =
                 positions.entry_intent_id
            WHERE positions.status IN (
                'opening',
                'open'
            )
              AND positions.quantity > 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM trade_intents exit_intent
                  WHERE exit_intent.position_id =
                        positions.id
                    AND exit_intent.intent_type IN (
                        'exit',
                        'emergency_exit'
                    )
                    AND exit_intent.status IN (
                        'created',
                        'approved',
                        'submitting',
                        'submitted',
                        'partially_filled'
                    )
                    AND exit_intent.expires_at > now()
              )
            ORDER BY positions.opened_at NULLS LAST,
                     positions.created_at
            LIMIT %s
            """,
            (limit,),
        )
        return cursor.fetchall()

    def load_position_legs(self, cursor, position_id):
        cursor.execute(
            """
            SELECT *
            FROM position_legs
            WHERE position_id = %s
            ORDER BY contract_symbol, side
            """,
            (position_id,),
        )
        return cursor.fetchall()

    def has_earnings_soon(self, cursor, symbol, now_date):
        end_date = now_date + timedelta(
            days=EARNINGS_EXIT_DAYS
        )
        cursor.execute(
            """
            SELECT report_date, report_time
            FROM earnings_events
            WHERE symbol = %s
              AND status = 'scheduled'
              AND report_date BETWEEN %s AND %s
            ORDER BY report_date
            LIMIT 1
            """,
            (
                symbol,
                now_date,
                end_date,
            ),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def stock_trigger(self, position, kill_switch):
        if kill_switch:
            return (
                "emergency_exit",
                "portfolio_kill_switch",
            )

        current = D(position["current_price"])
        stop = D(position["stop_loss_price"])
        target = D(position["take_profit_price"])

        if current is None:
            return None

        if position["direction"] == "long":
            if stop is not None and current <= stop:
                return "exit", "stop_loss"
            if target is not None and current >= target:
                return "exit", "take_profit"

        else:
            if stop is not None and current >= stop:
                return "exit", "stop_loss"
            if target is not None and current <= target:
                return "exit", "take_profit"

        return None

    def option_quotes(self, symbols):
        response = self.option_data.get_option_latest_quote(
            OptionLatestQuoteRequest(
                symbol_or_symbols=symbols
            )
        )

        result = {}
        for symbol in symbols:
            quote = response.get(symbol)
            if quote is None:
                continue

            bid = D(
                getattr(quote, "bid_price", None)
            )
            ask = D(
                getattr(quote, "ask_price", None)
            )

            if bid is None or ask is None:
                continue

            result[symbol] = {
                "bid": bid,
                "ask": ask,
                "mid": q2((bid + ask) / 2),
                "raw": model_payload(quote),
            }

        return result

    def option_state(
        self,
        cursor,
        position,
        legs,
        now,
    ):
        if len(legs) != 2:
            raise RuntimeError(
                f"Position {position['id']} does not "
                "have exactly two option legs"
            )

        symbols = [
            row["contract_symbol"]
            for row in legs
        ]
        quotes = self.option_quotes(symbols)

        if len(quotes) != 2:
            return {
                "quote_complete": False,
                "reason": "missing_option_quote",
            }

        short_leg = next(
            (
                row for row in legs
                if row["side"] == "short"
            ),
            None,
        )
        long_leg = next(
            (
                row for row in legs
                if row["side"] == "long"
            ),
            None,
        )

        if short_leg is None or long_leg is None:
            raise RuntimeError(
                f"Position {position['id']} is not a "
                "one-long/one-short spread"
            )

        short_quote = quotes[
            short_leg["contract_symbol"]
        ]
        long_quote = quotes[
            long_leg["contract_symbol"]
        ]

        # Conservative debit required to close:
        # buy short leg at ask, sell long leg at bid.
        close_debit = (
            short_quote["ask"]
            - long_quote["bid"]
        )
        close_debit = max(
            Decimal("0.01"),
            q2(close_debit),
        )

        expiration = min(
            occ_expiration(
                row["contract_symbol"]
            )
            for row in legs
        )
        dte = (expiration - now.date()).days

        initial_credit = abs(
            D(
                position["entry_limit_price"],
                Decimal("0"),
            )
        )

        if initial_credit <= 0:
            raise RuntimeError(
                f"Position {position['id']} has "
                "invalid initial credit"
            )

        earnings = self.has_earnings_soon(
            cursor,
            position["symbol"],
            now.date(),
        )

        return {
            "quote_complete": True,
            "quotes": quotes,
            "short_leg": short_leg,
            "long_leg": long_leg,
            "close_debit": close_debit,
            "initial_credit": initial_credit,
            "profit_target_debit":
                q2(
                    initial_credit
                    * OPTION_PROFIT_TARGET_FRACTION
                ),
            "loss_exit_debit":
                q2(
                    initial_credit
                    * OPTION_STOP_MULTIPLE
                ),
            "expiration": expiration,
            "dte": dte,
            "earnings": earnings,
        }

    def option_trigger(
        self,
        state,
        kill_switch,
    ):
        if kill_switch:
            return (
                "emergency_exit",
                "portfolio_kill_switch",
            )

        if not state["quote_complete"]:
            return None

        if (
            state["close_debit"]
            <= state["profit_target_debit"]
        ):
            return "exit", "option_50pct_credit_profit"

        if (
            state["close_debit"]
            >= state["loss_exit_debit"]
        ):
            return "exit", "option_2x_credit_loss"

        if state["dte"] <= OPTION_DTE_EXIT:
            return "exit", "option_3_dte"

        if state["earnings"] is not None:
            return "exit", "approaching_earnings"

        return None

    def idempotency_key(
        self,
        position,
        intent_type,
        reason,
    ):
        return (
            f"deltax_exit_v1:"
            f"{position['id']}:"
            f"{intent_type}:"
            f"{reason}"
        )

    def create_stock_intent(
        self,
        cursor,
        position,
        intent_type,
        reason,
        now,
    ):
        side = (
            "sell"
            if position["direction"] == "long"
            else "buy"
        )

        key = self.idempotency_key(
            position,
            intent_type,
            reason,
        )

        cursor.execute(
            """
            INSERT INTO trade_intents (
                trade_thesis_id,
                strategy_config_id,
                intent_type,
                asset_class,
                strategy,
                direction,
                symbol,
                side,
                quantity,
                order_type,
                time_in_force,
                limit_price,
                planned_entry_price,
                stop_loss_price,
                take_profit_price,
                premium_type,
                net_premium,
                max_profit,
                max_loss,
                idempotency_key,
                status,
                expires_at,
                metadata,
                position_id
            )
            VALUES (
                %s, %s, %s, 'stock',
                %s, %s, %s, %s, %s,
                'market', 'day',
                NULL, NULL, NULL, NULL,
                'none', NULL, NULL, 0,
                %s, 'approved', %s, %s, %s
            )
            ON CONFLICT (idempotency_key)
            DO NOTHING
            RETURNING id
            """,
            (
                position["trade_thesis_id"],
                position["strategy_config_id"],
                intent_type,
                position["strategy"],
                position["direction"],
                position["symbol"],
                side,
                position["quantity"],
                key,
                now + timedelta(
                    minutes=INTENT_TTL_MINUTES
                ),
                Jsonb(
                    {
                        "exit_reason": reason,
                        "position_id":
                            str(position["id"]),
                        "current_price":
                            str(
                                position[
                                    "current_price"
                                ]
                            ),
                    }
                ),
                position["id"],
            ),
        )
        row = cursor.fetchone()
        return row["id"] if row else None

    def create_option_intent(
        self,
        cursor,
        position,
        legs,
        state,
        intent_type,
        reason,
        now,
    ):
        if not state["quote_complete"]:
            return None

        close_debit = state["close_debit"]
        contracts = D(
            position["quantity"],
            Decimal("0"),
        )

        if contracts <= 0:
            return None

        total_debit = (
            close_debit
            * contracts
            * Decimal("100")
        )

        key = self.idempotency_key(
            position,
            intent_type,
            reason,
        )

        cursor.execute(
            """
            INSERT INTO trade_intents (
                trade_thesis_id,
                strategy_config_id,
                intent_type,
                asset_class,
                strategy,
                direction,
                symbol,
                side,
                quantity,
                order_type,
                time_in_force,
                limit_price,
                planned_entry_price,
                stop_loss_price,
                take_profit_price,
                premium_type,
                net_premium,
                max_profit,
                max_loss,
                idempotency_key,
                status,
                expires_at,
                metadata,
                position_id
            )
            VALUES (
                %s, %s, %s, 'option_spread',
                %s, %s, %s, NULL, %s,
                'limit', 'day',
                %s, NULL, NULL, NULL,
                'debit', %s, NULL, 0,
                %s, 'approved', %s, %s, %s
            )
            ON CONFLICT (idempotency_key)
            DO NOTHING
            RETURNING id
            """,
            (
                position["trade_thesis_id"],
                position["strategy_config_id"],
                intent_type,
                position["strategy"],
                position["direction"],
                position["symbol"],
                contracts,
                close_debit,
                total_debit,
                key,
                now + timedelta(
                    minutes=INTENT_TTL_MINUTES
                ),
                Jsonb(
                    {
                        "exit_reason": reason,
                        "position_id":
                            str(position["id"]),
                        "initial_credit_per_share":
                            str(
                                state[
                                    "initial_credit"
                                ]
                            ),
                        "estimated_close_debit_per_share":
                            str(close_debit),
                        "dte": state["dte"],
                        "expiration":
                            state[
                                "expiration"
                            ].isoformat(),
                        "earnings":
                            state["earnings"],
                    }
                ),
                position["id"],
            ),
        )
        row = cursor.fetchone()

        if row is None:
            return None

        intent_id = row["id"]

        # Close each existing position leg.
        ordered = sorted(
            legs,
            key=lambda x:
                (
                    0
                    if x["side"] == "short"
                    else 1,
                    x["contract_symbol"],
                ),
        )

        for index, leg in enumerate(
            ordered,
            start=1,
        ):
            quote = state["quotes"][
                leg["contract_symbol"]
            ]

            action = (
                "buy_to_close"
                if leg["side"] == "short"
                else "sell_to_close"
            )

            ratio = int(
                D(
                    leg["quantity"],
                    Decimal("1"),
                )
                / contracts
            )
            ratio = max(1, ratio)

            expiration = occ_expiration(
                leg["contract_symbol"]
            )

            # Option type and strike can be recovered
            # from OCC symbol for the audit record.
            match = re.search(
                r"(\d{6})([CP])(\d{8})$",
                leg["contract_symbol"],
            )
            option_type = (
                "call"
                if match.group(2) == "C"
                else "put"
            )
            strike = (
                Decimal(match.group(3))
                / Decimal("1000")
            )

            cursor.execute(
                """
                INSERT INTO trade_intent_legs (
                    trade_intent_id,
                    leg_number,
                    option_quote_snapshot_id,
                    contract_symbol,
                    action,
                    ratio_quantity,
                    option_type,
                    strike,
                    expiration_date,
                    multiplier,
                    reference_bid,
                    reference_ask,
                    reference_mid
                )
                VALUES (
                    %s, %s, NULL, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s
                )
                """,
                (
                    intent_id,
                    index,
                    leg["contract_symbol"],
                    action,
                    ratio,
                    option_type,
                    strike,
                    expiration,
                    leg["multiplier"],
                    quote["bid"],
                    quote["ask"],
                    quote["mid"],
                ),
            )

        return intent_id

    def health_check(self):
        state = self.account_state()

        with psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.validate_schema(cursor)
                control = dict(
                    self.load_control(cursor)
                )

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS open_positions,
                        COUNT(*) FILTER (
                            WHERE asset_class='stock'
                        ) AS stock_positions,
                        COUNT(*) FILTER (
                            WHERE asset_class='option_spread'
                        ) AS option_positions
                    FROM positions
                    WHERE status IN (
                        'opening',
                        'open'
                    )
                      AND quantity > 0
                    """
                )
                counts = dict(
                    cursor.fetchone()
                )

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS active_exit_intents
                    FROM trade_intents
                    WHERE intent_type IN (
                        'exit',
                        'emergency_exit'
                    )
                      AND status IN (
                        'created',
                        'approved',
                        'submitting',
                        'submitted',
                        'partially_filled'
                    )
                      AND expires_at > now()
                    """
                )
                exits = dict(
                    cursor.fetchone()
                )

        return {
            "paper_client": True,
            "alpaca": state,
            "bot_control": control,
            "positions": counts,
            **exits,
            "rules": {
                "stock_stop_loss": True,
                "stock_take_profit": True,
                "option_profit_target_fraction":
                    OPTION_PROFIT_TARGET_FRACTION,
                "option_loss_exit_multiple":
                    OPTION_STOP_MULTIPLE,
                "option_dte_exit":
                    OPTION_DTE_EXIT,
                "earnings_exit_days":
                    EARNINGS_EXIT_DAYS,
                "kill_switch_emergency_exit":
                    True,
                "option_close_price":
                    "short_ask_minus_long_bid",
            },
            "writes_performed": False,
            "broker_orders_submitted": False,
        }

    def process(self, limit):
        now = datetime.now(timezone.utc)
        state = self.account_state()

        with psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.validate_schema(cursor)
                control = self.load_control(cursor)
                positions = self.load_open_positions(
                    cursor,
                    limit,
                )

            results = []

            for position in positions:
                try:
                    with connection.cursor() as cursor:
                        if (
                            position["asset_class"]
                            == "stock"
                        ):
                            trigger = self.stock_trigger(
                                position,
                                control[
                                    "kill_switch_active"
                                ],
                            )

                            if trigger is None:
                                results.append(
                                    {
                                        "position_id":
                                            str(
                                                position["id"]
                                            ),
                                        "symbol":
                                            position[
                                                "symbol"
                                            ],
                                        "asset_class":
                                            "stock",
                                        "action":
                                            "hold",
                                    }
                                )
                                continue

                            intent_type, reason = trigger
                            intent_id = (
                                self.create_stock_intent(
                                    cursor,
                                    position,
                                    intent_type,
                                    reason,
                                    now,
                                )
                            )

                        else:
                            legs = (
                                self.load_position_legs(
                                    cursor,
                                    position["id"],
                                )
                            )

                            option_state = (
                                self.option_state(
                                    cursor,
                                    position,
                                    legs,
                                    now,
                                )
                            )

                            trigger = (
                                self.option_trigger(
                                    option_state,
                                    control[
                                        "kill_switch_active"
                                    ],
                                )
                            )

                            if trigger is None:
                                results.append(
                                    {
                                        "position_id":
                                            str(
                                                position["id"]
                                            ),
                                        "symbol":
                                            position[
                                                "symbol"
                                            ],
                                        "asset_class":
                                            "option_spread",
                                        "action":
                                            "hold",
                                        "state":
                                            option_state,
                                    }
                                )
                                continue

                            intent_type, reason = trigger
                            intent_id = (
                                self.create_option_intent(
                                    cursor,
                                    position,
                                    legs,
                                    option_state,
                                    intent_type,
                                    reason,
                                    now,
                                )
                            )

                        connection.commit()

                    results.append(
                        {
                            "position_id":
                                str(position["id"]),
                            "symbol":
                                position["symbol"],
                            "asset_class":
                                position[
                                    "asset_class"
                                ],
                            "action":
                                "intent_created"
                                if intent_id
                                else "duplicate_or_skipped",
                            "intent_id":
                                (
                                    str(intent_id)
                                    if intent_id
                                    else None
                                ),
                            "intent_type":
                                intent_type,
                            "reason":
                                reason,
                        }
                    )

                except Exception as error:
                    connection.rollback()
                    results.append(
                        {
                            "position_id":
                                str(position["id"]),
                            "symbol":
                                position["symbol"],
                            "asset_class":
                                position[
                                    "asset_class"
                                ],
                            "action":
                                "error",
                            "error":
                                str(error),
                        }
                    )

            return {
                "status": "completed",
                "selected_positions":
                    len(positions),
                "exit_intents_created":
                    sum(
                        1
                        for row in results
                        if row.get("action")
                        == "intent_created"
                    ),
                "results": results,
                "database_writes_performed":
                    any(
                        row.get("action")
                        == "intent_created"
                        for row in results
                    ),
                "broker_orders_submitted":
                    False,
            }


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Build deterministic DELTAX exit intents "
            "for open stock/options positions."
        )
    )
    mode = parser.add_mutually_exclusive_group(
        required=True
    )
    mode.add_argument(
        "--check",
        action="store_true",
    )
    mode.add_argument(
        "--process",
        action="store_true",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
    )
    args = parser.parse_args()

    if not 1 <= args.limit <= MAX_PROCESS_LIMIT:
        parser.error(
            f"--limit must be between 1 and "
            f"{MAX_PROCESS_LIMIT}"
        )

    return args


def main():
    args = parse_args()
    builder = ExitIntentBuilder()

    result = (
        builder.health_check()
        if args.check
        else builder.process(
            args.limit
        )
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=json_default,
        )
    )
    print("EXIT INTENT BUILDER: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
