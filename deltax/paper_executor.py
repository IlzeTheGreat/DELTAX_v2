# File: deltax/paper_executor.py
# DELTAX Alpaca PAPER executor v1.1
#
# Supports:
#   entry
#   exit
#   emergency_exit
#
# Safety model:
# - ENTRY requires execution_enabled=true, new_entries_enabled=true,
#   kill_switch_active=false.
# - Normal EXIT requires execution_enabled=true, but does NOT require
#   new_entries_enabled and is NOT blocked by kill switch.
# - EMERGENCY_EXIT bypasses execution_enabled/new_entries/kill-switch gates,
#   but still requires PAPER mode, open regular market and an unblocked Alpaca
#   account. This is necessary because the portfolio -5% kill switch disables
#   execution while simultaneously requiring risk positions to be closed.
# - Never uses live=True.

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    PositionIntent,
    TimeInForce,
)
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
)


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]

MAX_EXECUTE_LIMIT = 20
ACTIVE_INTENT_STATUS = "approved"
SUPPORTED_INTENT_TYPES = {
    "entry",
    "exit",
    "emergency_exit",
}


def json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def enum_value(value):
    if value is None:
        return None
    return getattr(value, "value", str(value))


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


def D(value, default=None):
    if value is None:
        return default
    return Decimal(str(value))


def as_float(value):
    if value is None:
        return None
    return float(Decimal(str(value)))


def safe_client_order_id(intent):
    asset = (
        "stk"
        if intent["asset_class"] == "stock"
        else "opt"
    )
    intent_code = {
        "entry": "en",
        "exit": "ex",
        "emergency_exit": "em",
    }[intent["intent_type"]]
    strategy = re.sub(
        r"[^a-z0-9]",
        "",
        intent["strategy"].lower(),
    )[:4]
    symbol = re.sub(
        r"[^A-Z0-9]",
        "",
        intent["symbol"].upper(),
    )[:8]
    suffix = str(intent["id"]).replace("-", "")[:14]
    return (
        f"dx-{intent_code}-{asset}-"
        f"{strategy}-{symbol}-{suffix}"
    )


def order_side(value):
    if value == "buy":
        return OrderSide.BUY
    if value == "sell":
        return OrderSide.SELL
    raise ValueError(
        f"Unsupported stock side: {value}"
    )


def option_position_intent(action):
    mapping = {
        "buy_to_open": PositionIntent.BUY_TO_OPEN,
        "sell_to_open": PositionIntent.SELL_TO_OPEN,
        "buy_to_close": PositionIntent.BUY_TO_CLOSE,
        "sell_to_close": PositionIntent.SELL_TO_CLOSE,
    }
    try:
        return mapping[action]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported option leg action: {action}"
        ) from exc


def option_side_from_action(action):
    if action in {
        "buy_to_open",
        "buy_to_close",
    }:
        return OrderSide.BUY
    if action in {
        "sell_to_open",
        "sell_to_close",
    }:
        return OrderSide.SELL
    raise ValueError(
        f"Unsupported option leg action: {action}"
    )


class PaperExecutor:
    def __init__(self, database_url=DATABASE_URL):
        self.database_url = database_url
        self.client = TradingClient(
            ALPACA_API_KEY,
            ALPACA_API_SECRET,
            paper=True,
        )

    def load_control(self, cursor):
        cursor.execute(
            """
            SELECT
                trading_mode,
                execution_enabled,
                new_entries_enabled,
                kill_switch_active,
                kill_switch_reason,
                last_heartbeat_at,
                updated_at
            FROM bot_control
            WHERE id = 1
            """
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                "bot_control row id=1 is missing"
            )
        return row

    def account_state(self):
        account = self.client.get_account()
        clock = self.client.get_clock()

        return {
            "account": account,
            "clock": clock,
            "account_status": str(
                getattr(account, "status", "")
            ),
            "trading_blocked": bool(
                getattr(
                    account,
                    "trading_blocked",
                    False,
                )
            ),
            "account_blocked": bool(
                getattr(
                    account,
                    "account_blocked",
                    False,
                )
            ),
            "equity": getattr(
                account,
                "equity",
                None,
            ),
            "buying_power": getattr(
                account,
                "buying_power",
                None,
            ),
            "options_buying_power": getattr(
                account,
                "options_buying_power",
                None,
            ),
            "options_approved_level": getattr(
                account,
                "options_approved_level",
                None,
            ),
            "options_trading_level": getattr(
                account,
                "options_trading_level",
                None,
            ),
        }

    def base_execution_failures(
        self,
        control,
        state,
    ):
        failures = []

        if control["trading_mode"] != "paper":
            failures.append(
                "trading_mode_not_paper"
            )

        if state["account_blocked"]:
            failures.append(
                "alpaca_account_blocked"
            )

        if state["trading_blocked"]:
            failures.append(
                "alpaca_trading_blocked"
            )

        if not bool(state["clock"].is_open):
            failures.append(
                "regular_market_not_open"
            )

        return failures

    def intent_gate_failures(
        self,
        intent,
        control,
        state,
    ):
        failures = self.base_execution_failures(
            control,
            state,
        )
        intent_type = intent["intent_type"]

        if intent_type == "entry":
            if (
                control["execution_enabled"]
                is not True
            ):
                failures.append(
                    "execution_disabled"
                )
            if (
                control["new_entries_enabled"]
                is not True
            ):
                failures.append(
                    "new_entries_disabled"
                )
            if (
                control["kill_switch_active"]
                is True
            ):
                failures.append(
                    "kill_switch_active"
                )

        elif intent_type == "exit":
            if (
                control["execution_enabled"]
                is not True
            ):
                failures.append(
                    "execution_disabled"
                )

        elif intent_type == "emergency_exit":
            # Deliberately bypass execution_enabled,
            # new_entries_enabled and kill-switch state.
            pass

        else:
            failures.append(
                "unsupported_intent_type"
            )

        return failures

    def load_pending_intents(
        self,
        cursor,
        now,
        limit,
    ):
        cursor.execute(
            """
            SELECT intents.*
            FROM trade_intents intents
            WHERE intents.status = %s
              AND intents.intent_type = ANY(%s)
              AND intents.expires_at > %s
              AND intents.asset_class IN (
                  'stock',
                  'option_spread'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM broker_orders broker
                  WHERE broker.trade_intent_id =
                        intents.id
                    AND broker.status NOT IN (
                        'failed',
                        'cancelled',
                        'canceled'
                    )
              )
            ORDER BY
                CASE intents.intent_type
                    WHEN 'emergency_exit' THEN 0
                    WHEN 'exit' THEN 1
                    ELSE 2
                END,
                intents.created_at,
                intents.id
            LIMIT %s
            """,
            (
                ACTIVE_INTENT_STATUS,
                list(SUPPORTED_INTENT_TYPES),
                now,
                limit,
            ),
        )
        return cursor.fetchall()

    def load_legs(
        self,
        cursor,
        intent_id,
    ):
        cursor.execute(
            """
            SELECT *
            FROM trade_intent_legs
            WHERE trade_intent_id = %s
            ORDER BY leg_number
            """,
            (intent_id,),
        )
        return cursor.fetchall()

    def validate_intent(
        self,
        intent,
        legs,
        now,
    ):
        failures = []

        if intent["expires_at"] <= now:
            failures.append(
                "intent_expired"
            )

        if intent["status"] != "approved":
            failures.append(
                "intent_not_approved"
            )

        if (
            intent["intent_type"]
            not in SUPPORTED_INTENT_TYPES
        ):
            failures.append(
                "intent_type_invalid"
            )

        if (
            intent["quantity"] is None
            or Decimal(
                str(intent["quantity"])
            ) <= 0
        ):
            failures.append(
                "quantity_missing_or_invalid"
            )

        if intent["asset_class"] == "stock":
            if intent["order_type"] != "market":
                failures.append(
                    "stock_must_be_market_order"
                )
            if intent["side"] not in {
                "buy",
                "sell",
            }:
                failures.append(
                    "stock_side_invalid"
                )
            if legs:
                failures.append(
                    "stock_intent_must_not_have_option_legs"
                )

        elif (
            intent["asset_class"]
            == "option_spread"
        ):
            if intent["strategy"] not in {
                "core",
                "active",
            }:
                failures.append(
                    "option_spread_strategy_invalid"
                )

            if intent["order_type"] != "limit":
                failures.append(
                    "option_spread_must_be_limit_order"
                )

            if intent["limit_price"] is None:
                failures.append(
                    "option_limit_price_missing"
                )

            if len(legs) != 2:
                failures.append(
                    "option_spread_requires_exactly_two_legs"
                )

            expiries = {
                row["expiration_date"]
                for row in legs
            }
            if len(expiries) != 1:
                failures.append(
                    "option_legs_must_share_expiration"
                )

            actions = {
                row["action"]
                for row in legs
            }

            if intent["intent_type"] == "entry":
                if (
                    intent["limit_price"]
                    is not None
                    and Decimal(
                        str(intent["limit_price"])
                    ) >= 0
                ):
                    failures.append(
                        "credit_spread_entry_limit_must_be_negative"
                    )

                if (
                    intent["premium_type"]
                    != "credit"
                ):
                    failures.append(
                        "option_entry_premium_type_must_be_credit"
                    )

                if actions != {
                    "sell_to_open",
                    "buy_to_open",
                }:
                    failures.append(
                        "option_entry_leg_actions_invalid"
                    )

            else:
                if (
                    intent["limit_price"]
                    is not None
                    and Decimal(
                        str(intent["limit_price"])
                    ) <= 0
                ):
                    failures.append(
                        "spread_exit_limit_must_be_positive_debit"
                    )

                if (
                    intent["premium_type"]
                    != "debit"
                ):
                    failures.append(
                        "option_exit_premium_type_must_be_debit"
                    )

                if actions != {
                    "buy_to_close",
                    "sell_to_close",
                }:
                    failures.append(
                        "option_exit_leg_actions_invalid"
                    )

        else:
            failures.append(
                "unsupported_asset_class"
            )

        return failures

    def reserve_broker_order(
        self,
        cursor,
        intent,
        client_order_id,
    ):
        asset_class = intent["asset_class"]
        order_class = (
            "simple"
            if asset_class == "stock"
            else "mleg"
        )

        cursor.execute(
            """
            INSERT INTO broker_orders (
                trade_intent_id,
                alpaca_order_id,
                client_order_id,
                parent_alpaca_order_id,
                asset_class,
                order_class,
                order_type,
                time_in_force,
                side,
                quantity,
                limit_price,
                status,
                filled_quantity,
                last_synced_at,
                raw_payload
            )
            VALUES (
                %s,
                NULL,
                %s,
                NULL,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                'submitting',
                0,
                now(),
                '{}'::jsonb
            )
            ON CONFLICT (client_order_id)
            DO NOTHING
            RETURNING id
            """,
            (
                intent["id"],
                client_order_id,
                asset_class,
                order_class,
                intent["order_type"],
                intent["time_in_force"],
                intent["side"],
                intent["quantity"],
                intent["limit_price"],
            ),
        )
        row = cursor.fetchone()
        return (
            row["id"]
            if row
            else None
        )

    def reserve_broker_legs(
        self,
        cursor,
        broker_order_id,
        legs,
    ):
        for leg in legs:
            side = enum_value(
                option_side_from_action(
                    leg["action"]
                )
            )

            cursor.execute(
                """
                INSERT INTO broker_order_legs (
                    broker_order_id,
                    alpaca_leg_order_id,
                    contract_symbol,
                    side,
                    ratio_quantity,
                    status,
                    filled_quantity,
                    raw_payload
                )
                VALUES (
                    %s,
                    NULL,
                    %s,
                    %s,
                    %s,
                    'submitting',
                    0,
                    %s
                )
                ON CONFLICT (
                    broker_order_id,
                    contract_symbol,
                    side
                )
                DO NOTHING
                """,
                (
                    broker_order_id,
                    leg["contract_symbol"],
                    side,
                    leg["ratio_quantity"],
                    Jsonb(
                        {
                            "trade_intent_leg_id":
                                str(leg["id"]),
                            "position_intent":
                                leg["action"],
                        }
                    ),
                ),
            )

    def mark_intent_submitting(
        self,
        cursor,
        intent_id,
    ):
        cursor.execute(
            """
            UPDATE trade_intents
            SET
                status = 'submitting',
                updated_at = now()
            WHERE id = %s
              AND status = 'approved'
            """,
            (intent_id,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "Could not reserve approved "
                f"intent {intent_id}"
            )

    def build_stock_request(
        self,
        intent,
        client_order_id,
    ):
        return MarketOrderRequest(
            symbol=intent["symbol"],
            qty=as_float(
                intent["quantity"]
            ),
            side=order_side(
                intent["side"]
            ),
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )

    def build_options_request(
        self,
        intent,
        legs,
        client_order_id,
    ):
        alpaca_legs = []

        for leg in legs:
            alpaca_legs.append(
                OptionLegRequest(
                    symbol=
                        leg["contract_symbol"],
                    ratio_qty=float(
                        leg["ratio_quantity"]
                    ),
                    side=
                        option_side_from_action(
                            leg["action"]
                        ),
                    position_intent=
                        option_position_intent(
                            leg["action"]
                        ),
                )
            )

        return LimitOrderRequest(
            qty=as_float(
                intent["quantity"]
            ),
            limit_price=as_float(
                intent["limit_price"]
            ),
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.MLEG,
            client_order_id=client_order_id,
            legs=alpaca_legs,
        )

    def build_request(
        self,
        intent,
        legs,
        client_order_id,
    ):
        if intent["asset_class"] == "stock":
            return self.build_stock_request(
                intent,
                client_order_id,
            )

        if (
            intent["asset_class"]
            == "option_spread"
        ):
            return self.build_options_request(
                intent,
                legs,
                client_order_id,
            )

        raise ValueError(
            "Unsupported asset_class "
            f"{intent['asset_class']}"
        )

    def complete_submission(
        self,
        cursor,
        intent,
        broker_order_id,
        order,
    ):
        payload = model_payload(order)

        cursor.execute(
            """
            UPDATE broker_orders
            SET
                alpaca_order_id = %s,
                parent_alpaca_order_id = %s,
                status = %s,
                filled_quantity = %s,
                filled_average_price = %s,
                submitted_at = %s,
                filled_at = %s,
                cancelled_at = %s,
                failed_at = %s,
                last_synced_at = now(),
                raw_payload = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (
                str(
                    getattr(order, "id", "")
                ) or None,
                (
                    str(
                        getattr(
                            order,
                            "parent_order_id",
                            "",
                        )
                    )
                    if getattr(
                        order,
                        "parent_order_id",
                        None,
                    )
                    else None
                ),
                enum_value(
                    getattr(
                        order,
                        "status",
                        None,
                    )
                ) or "submitted",
                D(
                    getattr(
                        order,
                        "filled_qty",
                        0,
                    ),
                    Decimal("0"),
                ),
                D(
                    getattr(
                        order,
                        "filled_avg_price",
                        None,
                    )
                ),
                getattr(
                    order,
                    "submitted_at",
                    None,
                ),
                getattr(
                    order,
                    "filled_at",
                    None,
                ),
                getattr(
                    order,
                    "canceled_at",
                    None,
                ),
                getattr(
                    order,
                    "failed_at",
                    None,
                ),
                Jsonb(payload),
                broker_order_id,
            ),
        )

        broker_status = (
            enum_value(
                getattr(
                    order,
                    "status",
                    None,
                )
            )
            or "submitted"
        )

        intent_status = (
            "filled"
            if broker_status == "filled"
            else "partially_filled"
            if broker_status
            == "partially_filled"
            else "submitted"
        )

        cursor.execute(
            """
            UPDATE trade_intents
            SET
                status = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (
                intent_status,
                intent["id"],
            ),
        )

        cursor.execute(
            """
            INSERT INTO broker_order_events (
                broker_order_id,
                event_type,
                broker_event_at,
                payload
            )
            VALUES (
                %s,
                'submitted',
                %s,
                %s
            )
            """,
            (
                broker_order_id,
                getattr(
                    order,
                    "submitted_at",
                    None,
                ),
                Jsonb(payload),
            ),
        )

        api_legs = (
            getattr(
                order,
                "legs",
                None,
            )
            or []
        )

        for api_leg in api_legs:
            leg_symbol = getattr(
                api_leg,
                "symbol",
                None,
            )
            leg_side = enum_value(
                getattr(
                    api_leg,
                    "side",
                    None,
                )
            )
            if not leg_symbol or not leg_side:
                continue

            cursor.execute(
                """
                UPDATE broker_order_legs
                SET
                    alpaca_leg_order_id = %s,
                    status = %s,
                    filled_quantity = %s,
                    filled_average_price = %s,
                    raw_payload = %s
                WHERE broker_order_id = %s
                  AND contract_symbol = %s
                  AND side = %s
                """,
                (
                    str(
                        getattr(
                            api_leg,
                            "id",
                            "",
                        )
                    ) or None,
                    enum_value(
                        getattr(
                            api_leg,
                            "status",
                            None,
                        )
                    ),
                    D(
                        getattr(
                            api_leg,
                            "filled_qty",
                            0,
                        ),
                        Decimal("0"),
                    ),
                    D(
                        getattr(
                            api_leg,
                            "filled_avg_price",
                            None,
                        )
                    ),
                    Jsonb(
                        model_payload(
                            api_leg
                        )
                    ),
                    broker_order_id,
                    leg_symbol,
                    leg_side,
                ),
            )

    def fail_submission(
        self,
        cursor,
        intent,
        broker_order_id,
        error,
    ):
        message = str(error)[:4000]

        cursor.execute(
            """
            UPDATE broker_orders
            SET
                status = 'failed',
                failed_at = now(),
                last_synced_at = now(),
                raw_payload = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (
                Jsonb(
                    {
                        "submission_error":
                            message
                    }
                ),
                broker_order_id,
            ),
        )

        cursor.execute(
            """
            UPDATE trade_intents
            SET
                status = 'failed',
                updated_at = now(),
                metadata =
                    metadata || %s
            WHERE id = %s
            """,
            (
                Jsonb(
                    {
                        "execution_error":
                            message
                    }
                ),
                intent["id"],
            ),
        )

        cursor.execute(
            """
            INSERT INTO broker_order_events (
                broker_order_id,
                event_type,
                broker_event_at,
                payload
            )
            VALUES (
                %s,
                'submission_failed',
                now(),
                %s
            )
            """,
            (
                broker_order_id,
                Jsonb(
                    {
                        "error":
                            message
                    }
                ),
            ),
        )

        cursor.execute(
            """
            INSERT INTO risk_events (
                severity,
                event_code,
                symbol,
                trade_intent_id,
                message,
                details
            )
            VALUES (
                'critical',
                'broker_submission_failed',
                %s,
                %s,
                %s,
                %s
            )
            """,
            (
                intent["symbol"],
                intent["id"],
                (
                    "Broker submission failed "
                    f"for {intent['intent_type']} "
                    f"{intent['asset_class']} "
                    f"{intent['symbol']}"
                ),
                Jsonb(
                    {
                        "error":
                            message
                    }
                ),
            ),
        )

    def health_check(self):
        now = datetime.now(
            timezone.utc
        )
        state = self.account_state()

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                control = self.load_control(
                    cursor
                )
                pending = (
                    self.load_pending_intents(
                        cursor,
                        now,
                        MAX_EXECUTE_LIMIT,
                    )
                )

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS broker_orders,
                        COUNT(*) FILTER (
                            WHERE status =
                                  'submitting'
                        ) AS submitting,
                        COUNT(*) FILTER (
                            WHERE status =
                                  'failed'
                        ) AS failed
                    FROM broker_orders
                    """
                )
                counts = cursor.fetchone()

        return {
            "bot_control": dict(control),
            "alpaca": {
                "paper_client": True,
                "account_status":
                    state["account_status"],
                "market_open": bool(
                    state["clock"].is_open
                ),
                "clock_timestamp":
                    state["clock"].timestamp,
                "trading_blocked":
                    state["trading_blocked"],
                "account_blocked":
                    state["account_blocked"],
                "equity":
                    state["equity"],
                "buying_power":
                    state["buying_power"],
                "options_buying_power":
                    state[
                        "options_buying_power"
                    ],
                "options_approved_level":
                    state[
                        "options_approved_level"
                    ],
                "options_trading_level":
                    state[
                        "options_trading_level"
                    ],
            },
            "base_execution_gate_failures":
                self.base_execution_failures(
                    control,
                    state,
                ),
            "approved_unsubmitted_intents": [
                {
                    "id": str(row["id"]),
                    "intent_type":
                        row["intent_type"],
                    "asset_class":
                        row["asset_class"],
                    "symbol":
                        row["symbol"],
                    "strategy":
                        row["strategy"],
                    "direction":
                        row["direction"],
                    "quantity":
                        row["quantity"],
                    "order_type":
                        row["order_type"],
                    "limit_price":
                        row["limit_price"],
                    "expires_at":
                        row["expires_at"],
                    "gate_failures":
                        self.intent_gate_failures(
                            row,
                            control,
                            state,
                        ),
                }
                for row in pending
            ],
            "broker_order_counts":
                dict(counts),
            "rules": {
                "entry_requires_execution_enabled":
                    True,
                "entry_requires_new_entries_enabled":
                    True,
                "entry_blocked_by_kill_switch":
                    True,
                "normal_exit_requires_execution_enabled":
                    True,
                "emergency_exit_bypasses_execution_enabled":
                    True,
                "emergency_exit_bypasses_kill_switch":
                    True,
                "all_orders_paper_only":
                    True,
            },
            "writes_performed":
                False,
            "broker_orders_submitted":
                False,
        }

    def execute(self, limit):
        now = datetime.now(
            timezone.utc
        )
        state = self.account_state()

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                control = self.load_control(
                    cursor
                )
                intents = (
                    self.load_pending_intents(
                        cursor,
                        now,
                        limit,
                    )
                )

            results = []
            submitted_count = 0

            for intent in intents:
                gate_failures = (
                    self.intent_gate_failures(
                        intent,
                        control,
                        state,
                    )
                )

                if gate_failures:
                    results.append(
                        {
                            "intent_id":
                                str(intent["id"]),
                            "intent_type":
                                intent[
                                    "intent_type"
                                ],
                            "symbol":
                                intent["symbol"],
                            "status":
                                "blocked",
                            "failures":
                                gate_failures,
                        }
                    )
                    continue

                client_order_id = (
                    safe_client_order_id(
                        intent
                    )
                )

                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT *
                        FROM trade_intents
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (intent["id"],),
                    )
                    locked = cursor.fetchone()

                    if locked is None:
                        connection.rollback()
                        continue

                    legs = self.load_legs(
                        cursor,
                        locked["id"],
                    )

                    failures = (
                        self.validate_intent(
                            locked,
                            legs,
                            now,
                        )
                    )

                    if failures:
                        cursor.execute(
                            """
                            UPDATE trade_intents
                            SET
                                status =
                                    'rejected',
                                updated_at =
                                    now(),
                                metadata =
                                    metadata || %s
                            WHERE id = %s
                            """,
                            (
                                Jsonb(
                                    {
                                        "execution_rejection":
                                            failures
                                    }
                                ),
                                locked["id"],
                            ),
                        )
                        cursor.execute(
                            """
                            INSERT INTO risk_events (
                                severity,
                                event_code,
                                symbol,
                                trade_intent_id,
                                message,
                                details
                            )
                            VALUES (
                                'critical',
                                'execution_intent_invalid',
                                %s,
                                %s,
                                %s,
                                %s
                            )
                            """,
                            (
                                locked["symbol"],
                                locked["id"],
                                (
                                    "Approved intent "
                                    "failed final "
                                    "execution validation"
                                ),
                                Jsonb(
                                    {
                                        "failures":
                                            failures
                                    }
                                ),
                            ),
                        )
                        connection.commit()

                        results.append(
                            {
                                "intent_id":
                                    str(
                                        locked["id"]
                                    ),
                                "intent_type":
                                    locked[
                                        "intent_type"
                                    ],
                                "symbol":
                                    locked["symbol"],
                                "status":
                                    "rejected",
                                "failures":
                                    failures,
                            }
                        )
                        continue

                    broker_order_id = (
                        self.reserve_broker_order(
                            cursor,
                            locked,
                            client_order_id,
                        )
                    )

                    if broker_order_id is None:
                        connection.rollback()
                        results.append(
                            {
                                "intent_id":
                                    str(
                                        locked["id"]
                                    ),
                                "symbol":
                                    locked["symbol"],
                                "status":
                                    "skipped_duplicate_client_order_id",
                            }
                        )
                        continue

                    if (
                        locked["asset_class"]
                        == "option_spread"
                    ):
                        self.reserve_broker_legs(
                            cursor,
                            broker_order_id,
                            legs,
                        )

                    self.mark_intent_submitting(
                        cursor,
                        locked["id"],
                    )

                    connection.commit()

                request = self.build_request(
                    locked,
                    legs,
                    client_order_id,
                )

                try:
                    order = (
                        self.client.submit_order(
                            order_data=request
                        )
                    )

                    with connection.cursor() as cursor:
                        self.complete_submission(
                            cursor,
                            locked,
                            broker_order_id,
                            order,
                        )
                        connection.commit()

                    submitted_count += 1

                    results.append(
                        {
                            "intent_id":
                                str(locked["id"]),
                            "intent_type":
                                locked[
                                    "intent_type"
                                ],
                            "broker_order_id":
                                str(
                                    broker_order_id
                                ),
                            "alpaca_order_id":
                                str(
                                    getattr(
                                        order,
                                        "id",
                                        "",
                                    )
                                ),
                            "client_order_id":
                                client_order_id,
                            "asset_class":
                                locked[
                                    "asset_class"
                                ],
                            "symbol":
                                locked["symbol"],
                            "status":
                                enum_value(
                                    getattr(
                                        order,
                                        "status",
                                        None,
                                    )
                                ),
                        }
                    )

                except Exception as error:
                    with connection.cursor() as cursor:
                        self.fail_submission(
                            cursor,
                            locked,
                            broker_order_id,
                            error,
                        )
                        connection.commit()

                    results.append(
                        {
                            "intent_id":
                                str(locked["id"]),
                            "intent_type":
                                locked[
                                    "intent_type"
                                ],
                            "broker_order_id":
                                str(
                                    broker_order_id
                                ),
                            "client_order_id":
                                client_order_id,
                            "symbol":
                                locked["symbol"],
                            "status":
                                "failed",
                            "error":
                                str(error),
                        }
                    )

            return {
                "status": "completed",
                "selected":
                    len(intents),
                "results":
                    results,
                "broker_orders_submitted":
                    submitted_count,
            }


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "DELTAX Alpaca PAPER "
            "trade-intent executor."
        )
    )

    mode = (
        parser.add_mutually_exclusive_group(
            required=True
        )
    )
    mode.add_argument(
        "--check",
        action="store_true",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=5,
    )

    args = parser.parse_args()

    if not 1 <= args.limit <= MAX_EXECUTE_LIMIT:
        parser.error(
            "--limit must be between 1 "
            f"and {MAX_EXECUTE_LIMIT}"
        )

    return args


def main():
    args = parse_args()
    executor = PaperExecutor()

    result = (
        executor.health_check()
        if args.check
        else executor.execute(
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
    print("PAPER EXECUTOR: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
