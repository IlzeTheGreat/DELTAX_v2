# File: deltax/broker_order_reconciler.py
# Purpose: Synchronize DELTAX broker_orders with Alpaca PAPER and materialize
# positions from fills.
#
# Safety:
# - PAPER client only.
# - --check is read-only.
# - --sync never submits, replaces, or cancels orders.
# - Existing broker_orders are the source of which Alpaca orders are tracked.
# - Position creation is idempotent by entry_intent_id lookup.
#
# Usage:
#   python deltax/broker_order_reconciler.py --check
#   python deltax/broker_order_reconciler.py --sync
#   python deltax/broker_order_reconciler.py --sync --limit 50

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alpaca.trading.client import TradingClient


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]

MAX_SYNC_LIMIT = 200

TERMINAL_BROKER_STATUSES = {
    "filled",
    "canceled",
    "cancelled",
    "expired",
    "rejected",
}

TRACKED_INTENT_TYPES = {"entry", "exit", "emergency_exit"}


def json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def enum_value(value):
    if value is None:
        return None
    return getattr(value, "value", str(value))


def D(value, default=None):
    if value is None:
        return default
    return Decimal(str(value))


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


def normalize_broker_status(value):
    status = (enum_value(value) or "").lower()

    aliases = {
        "cancelled": "canceled",
    }
    return aliases.get(status, status)


def intent_status_for_broker(broker_status, filled_qty):
    status = normalize_broker_status(broker_status)
    filled_qty = D(filled_qty, Decimal("0"))

    if status == "filled":
        return "filled"

    if status == "partially_filled":
        return "partially_filled"

    if status == "expired":
        return "expired"

    if status in {"canceled", "done_for_day"}:
        # A canceled stock order can still have a partial fill.
        return (
            "partially_filled"
            if filled_qty > 0
            else "cancelled"
        )

    if status == "rejected":
        return "rejected"

    return "submitted"


def broker_event_at(order):
    for attr in (
        "filled_at",
        "canceled_at",
        "expired_at",
        "failed_at",
        "updated_at",
        "submitted_at",
        "created_at",
    ):
        value = getattr(order, attr, None)
        if value is not None:
            return value
    return None


class BrokerOrderReconciler:
    def __init__(self, database_url=DATABASE_URL):
        self.database_url = database_url
        self.client = TradingClient(
            ALPACA_API_KEY,
            ALPACA_API_SECRET,
            paper=True,
        )

    def validate_schema(self, cursor):
        required = {
            "broker_orders": {
                "id",
                "trade_intent_id",
                "alpaca_order_id",
                "client_order_id",
                "asset_class",
                "status",
                "filled_quantity",
                "filled_average_price",
                "submitted_at",
                "filled_at",
                "cancelled_at",
                "failed_at",
                "last_synced_at",
                "raw_payload",
                "updated_at",
            },
            "broker_order_legs": {
                "broker_order_id",
                "alpaca_leg_order_id",
                "contract_symbol",
                "side",
                "status",
                "filled_quantity",
                "filled_average_price",
                "raw_payload",
            },
            "broker_order_events": {
                "broker_order_id",
                "event_type",
                "broker_event_at",
                "payload",
            },
            "trade_intents": {
                "id",
                "trade_thesis_id",
                "intent_type",
                "asset_class",
                "strategy",
                "direction",
                "symbol",
                "quantity",
                "stop_loss_price",
                "take_profit_price",
                "max_loss",
                "status",
                "position_id",
                "metadata",
            },
            "trade_intent_legs": {
                "trade_intent_id",
                "contract_symbol",
                "action",
                "ratio_quantity",
                "multiplier",
            },
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
                "stop_loss_price",
                "take_profit_price",
                "initial_max_loss",
                "opened_at",
                "updated_at",
            },
            "position_legs": {
                "position_id",
                "contract_symbol",
                "side",
                "quantity",
                "multiplier",
                "average_entry_price",
                "raw_payload",
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
            actual.setdefault(row["table_name"], set()).add(
                row["column_name"]
            )

        missing = {
            table: sorted(columns - actual.get(table, set()))
            for table, columns in required.items()
            if columns - actual.get(table, set())
        }

        if missing:
            raise RuntimeError(
                f"Required reconciliation schema missing: {missing}"
            )

    def account_state(self):
        account = self.client.get_account()
        return {
            "status": str(getattr(account, "status", "")),
            "account_blocked": bool(
                getattr(account, "account_blocked", False)
            ),
            "trading_blocked": bool(
                getattr(account, "trading_blocked", False)
            ),
            "equity": getattr(account, "equity", None),
        }

    def load_orders_to_sync(self, cursor, limit):
        cursor.execute(
            """
            SELECT
                broker.*,
                intents.intent_type,
                intents.asset_class AS intent_asset_class,
                intents.symbol,
                intents.strategy,
                intents.direction,
                intents.status AS intent_status,
                intents.position_id
            FROM broker_orders broker
            JOIN trade_intents intents
              ON intents.id = broker.trade_intent_id
            WHERE (
                broker.status NOT IN (
                    'filled',
                    'canceled',
                    'cancelled',
                    'expired',
                    'rejected',
                    'failed'
                )
                OR (
                    broker.status = 'filled'
                    AND intents.position_id IS NULL
                    AND intents.intent_type = 'entry'
                )
            )
            ORDER BY broker.created_at, broker.id
            LIMIT %s
            """,
            (limit,),
        )
        return cursor.fetchall()

    def fetch_alpaca_order(self, broker_row):
        alpaca_order_id = broker_row["alpaca_order_id"]

        if alpaca_order_id:
            return self.client.get_order_by_id(
                alpaca_order_id,
                nested=True,
            )

        client_order_id = broker_row["client_order_id"]
        if not client_order_id:
            raise RuntimeError(
                f"Broker order {broker_row['id']} has neither "
                "alpaca_order_id nor client_order_id"
            )

        # Recovery path for a crash after Alpaca accepted the order but before
        # DELTAX persisted the returned Alpaca order id.
        return self.client.get_order_by_client_id(
            client_order_id,
        )

    def load_intent(self, cursor, intent_id):
        cursor.execute(
            """
            SELECT *
            FROM trade_intents
            WHERE id = %s
            FOR UPDATE
            """,
            (intent_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                f"Trade intent not found: {intent_id}"
            )
        return row

    def load_intent_legs(self, cursor, intent_id):
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

    def sync_broker_legs(self, cursor, broker_order_id, order):
        api_legs = getattr(order, "legs", None) or []
        changes = 0

        for api_leg in api_legs:
            symbol = getattr(api_leg, "symbol", None)
            side = enum_value(getattr(api_leg, "side", None))

            if not symbol or not side:
                continue

            cursor.execute(
                """
                SELECT
                    status,
                    filled_quantity,
                    filled_average_price
                FROM broker_order_legs
                WHERE broker_order_id = %s
                  AND contract_symbol = %s
                  AND side = %s
                """,
                (broker_order_id, symbol, side),
            )
            before = cursor.fetchone()

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
                    str(getattr(api_leg, "id", "")) or None,
                    normalize_broker_status(
                        getattr(api_leg, "status", None)
                    ),
                    D(
                        getattr(api_leg, "filled_qty", 0),
                        Decimal("0"),
                    ),
                    D(
                        getattr(api_leg, "filled_avg_price", None)
                    ),
                    Jsonb(model_payload(api_leg)),
                    broker_order_id,
                    symbol,
                    side,
                ),
            )

            if cursor.rowcount and before:
                after_status = normalize_broker_status(
                    getattr(api_leg, "status", None)
                )
                after_qty = D(
                    getattr(api_leg, "filled_qty", 0),
                    Decimal("0"),
                )
                after_avg = D(
                    getattr(api_leg, "filled_avg_price", None)
                )

                if (
                    before["status"] != after_status
                    or D(
                        before["filled_quantity"],
                        Decimal("0"),
                    ) != after_qty
                    or D(
                        before["filled_average_price"]
                    ) != after_avg
                ):
                    changes += 1

        return changes

    def ensure_entry_position(
        self,
        cursor,
        intent,
        broker_order,
        order,
    ):
        if intent["intent_type"] != "entry":
            return None, False

        filled_qty = D(
            getattr(order, "filled_qty", None),
            D(broker_order["filled_quantity"], Decimal("0")),
        )

        if filled_qty <= 0:
            return None, False

        cursor.execute(
            """
            SELECT *
            FROM positions
            WHERE entry_intent_id = %s
            ORDER BY created_at
            LIMIT 1
            FOR UPDATE
            """,
            (intent["id"],),
        )
        position = cursor.fetchone()

        broker_status = normalize_broker_status(
            getattr(order, "status", None)
        )
        position_status = (
            "open"
            if broker_status in {
                "filled",
                "canceled",
                "expired",
            }
            else "opening"
        )

        avg_price = D(
            getattr(order, "filled_avg_price", None),
            D(broker_order["filled_average_price"]),
        )
        opened_at = (
            getattr(order, "filled_at", None)
            or getattr(order, "updated_at", None)
            or getattr(order, "submitted_at", None)
        )

        created = False

        if position is None:
            cursor.execute(
                """
                INSERT INTO positions (
                    trade_thesis_id,
                    entry_intent_id,
                    symbol,
                    asset_class,
                    strategy,
                    direction,
                    status,
                    quantity,
                    average_entry_price,
                    current_price,
                    stop_loss_price,
                    take_profit_price,
                    initial_max_loss,
                    opened_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                RETURNING *
                """,
                (
                    intent["trade_thesis_id"],
                    intent["id"],
                    intent["symbol"],
                    intent["asset_class"],
                    intent["strategy"],
                    intent["direction"],
                    position_status,
                    filled_qty,
                    avg_price,
                    avg_price,
                    intent["stop_loss_price"],
                    intent["take_profit_price"],
                    intent["max_loss"],
                    opened_at,
                ),
            )
            position = cursor.fetchone()
            created = True
        else:
            cursor.execute(
                """
                UPDATE positions
                SET
                    status = %s,
                    quantity = %s,
                    average_entry_price = COALESCE(%s, average_entry_price),
                    current_price = COALESCE(%s, current_price),
                    stop_loss_price = COALESCE(
                        stop_loss_price, %s
                    ),
                    take_profit_price = COALESCE(
                        take_profit_price, %s
                    ),
                    opened_at = COALESCE(opened_at, %s),
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (
                    position_status,
                    filled_qty,
                    avg_price,
                    avg_price,
                    intent["stop_loss_price"],
                    intent["take_profit_price"],
                    opened_at,
                    position["id"],
                ),
            )
            position = cursor.fetchone()

        cursor.execute(
            """
            UPDATE trade_intents
            SET
                position_id = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (position["id"], intent["id"]),
        )

        if intent["asset_class"] == "option_spread":
            self.sync_position_legs(
                cursor,
                position,
                intent,
                order,
            )

        return position, created

    def sync_position_legs(
        self,
        cursor,
        position,
        intent,
        order,
    ):
        intent_legs = self.load_intent_legs(
            cursor,
            intent["id"],
        )
        api_legs = getattr(order, "legs", None) or []
        api_by_symbol = {
            getattr(leg, "symbol", None): leg
            for leg in api_legs
            if getattr(leg, "symbol", None)
        }

        parent_qty = D(
            getattr(order, "filled_qty", None),
            position["quantity"] or Decimal("0"),
        )

        for leg in intent_legs:
            api_leg = api_by_symbol.get(
                leg["contract_symbol"]
            )

            action = leg["action"]
            position_side = (
                "long"
                if action in {"buy_to_open", "buy_to_close"}
                else "short"
            )

            leg_qty = parent_qty * Decimal(
                str(leg["ratio_quantity"])
            )

            avg_price = (
                D(
                    getattr(
                        api_leg,
                        "filled_avg_price",
                        None,
                    )
                )
                if api_leg is not None
                else None
            )

            raw = (
                model_payload(api_leg)
                if api_leg is not None
                else {
                    "source": "trade_intent_leg",
                    "action": action,
                }
            )

            cursor.execute(
                """
                INSERT INTO position_legs (
                    position_id,
                    contract_symbol,
                    side,
                    quantity,
                    multiplier,
                    average_entry_price,
                    raw_payload
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (
                    position_id,
                    contract_symbol,
                    side
                )
                DO UPDATE SET
                    quantity = EXCLUDED.quantity,
                    multiplier = EXCLUDED.multiplier,
                    average_entry_price = COALESCE(
                        EXCLUDED.average_entry_price,
                        position_legs.average_entry_price
                    ),
                    raw_payload = EXCLUDED.raw_payload
                """,
                (
                    position["id"],
                    leg["contract_symbol"],
                    position_side,
                    leg_qty,
                    leg["multiplier"],
                    avg_price,
                    Jsonb(raw),
                ),
            )

    def update_one(
        self,
        cursor,
        broker_row,
        order,
    ):
        intent = self.load_intent(
            cursor,
            broker_row["trade_intent_id"],
        )

        if intent["intent_type"] not in TRACKED_INTENT_TYPES:
            raise RuntimeError(
                f"Unsupported intent_type: {intent['intent_type']}"
            )

        new_status = normalize_broker_status(
            getattr(order, "status", None)
        )
        new_filled_qty = D(
            getattr(order, "filled_qty", 0),
            Decimal("0"),
        )
        new_avg = D(
            getattr(order, "filled_avg_price", None)
        )
        payload = model_payload(order)

        previous_status = normalize_broker_status(
            broker_row["status"]
        )
        previous_filled_qty = D(
            broker_row["filled_quantity"],
            Decimal("0"),
        )
        previous_avg = D(
            broker_row["filled_average_price"]
        )

        changed = (
            previous_status != new_status
            or previous_filled_qty != new_filled_qty
            or previous_avg != new_avg
            or not broker_row["alpaca_order_id"]
        )

        cursor.execute(
            """
            UPDATE broker_orders
            SET
                alpaca_order_id = COALESCE(%s, alpaca_order_id),
                parent_alpaca_order_id = %s,
                status = %s,
                filled_quantity = %s,
                filled_average_price = %s,
                submitted_at = COALESCE(%s, submitted_at),
                filled_at = %s,
                cancelled_at = %s,
                failed_at = %s,
                last_synced_at = now(),
                raw_payload = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (
                str(getattr(order, "id", "")) or None,
                (
                    str(getattr(order, "parent_order_id", ""))
                    if getattr(order, "parent_order_id", None)
                    else None
                ),
                new_status or previous_status or "submitted",
                new_filled_qty,
                new_avg,
                getattr(order, "submitted_at", None),
                getattr(order, "filled_at", None),
                getattr(order, "canceled_at", None),
                getattr(order, "failed_at", None),
                Jsonb(payload),
                broker_row["id"],
            ),
        )

        new_intent_status = intent_status_for_broker(
            new_status,
            new_filled_qty,
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
                new_intent_status,
                intent["id"],
            ),
        )

        leg_changes = 0
        if intent["asset_class"] == "option_spread":
            leg_changes = self.sync_broker_legs(
                cursor,
                broker_row["id"],
                order,
            )

        position, position_created = self.ensure_entry_position(
            cursor,
            intent,
            broker_row,
            order,
        )

        if changed or leg_changes or position_created:
            cursor.execute(
                """
                INSERT INTO broker_order_events (
                    broker_order_id,
                    event_type,
                    broker_event_at,
                    payload
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    broker_row["id"],
                    (
                        "filled"
                        if new_status == "filled"
                        else "partially_filled"
                        if new_status == "partially_filled"
                        else "status_changed"
                    ),
                    broker_event_at(order),
                    Jsonb(
                        {
                            "previous_status":
                                previous_status,
                            "new_status": new_status,
                            "previous_filled_quantity":
                                str(previous_filled_qty),
                            "new_filled_quantity":
                                str(new_filled_qty),
                            "position_id": (
                                str(position["id"])
                                if position
                                else None
                            ),
                            "position_created":
                                position_created,
                            "alpaca": payload,
                        }
                    ),
                ),
            )

        return {
            "broker_order_id": str(broker_row["id"]),
            "trade_intent_id": str(intent["id"]),
            "symbol": intent["symbol"],
            "asset_class": intent["asset_class"],
            "previous_status": previous_status,
            "broker_status": new_status,
            "intent_status": new_intent_status,
            "filled_quantity": new_filled_qty,
            "filled_average_price": new_avg,
            "changed": changed,
            "leg_changes": leg_changes,
            "position_id": (
                str(position["id"])
                if position
                else None
            ),
            "position_created": position_created,
        }

    def health_check(self):
        account = self.account_state()

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.validate_schema(cursor)

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (
                            WHERE status NOT IN (
                                'filled',
                                'canceled',
                                'cancelled',
                                'expired',
                                'rejected',
                                'failed'
                            )
                        ) AS non_terminal,
                        COUNT(*) FILTER (
                            WHERE status = 'submitting'
                        ) AS submitting,
                        COUNT(*) FILTER (
                            WHERE alpaca_order_id IS NULL
                              AND status <> 'failed'
                        ) AS missing_alpaca_order_id
                    FROM broker_orders
                    """
                )
                counts = dict(cursor.fetchone())

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS positions,
                        COUNT(*) FILTER (
                            WHERE status = 'opening'
                        ) AS opening,
                        COUNT(*) FILTER (
                            WHERE status = 'open'
                        ) AS open
                    FROM positions
                    """
                )
                positions = dict(cursor.fetchone())

                cursor.execute(
                    """
                    SELECT COUNT(*) AS filled_entry_without_position
                    FROM trade_intents
                    WHERE intent_type = 'entry'
                      AND status IN (
                          'filled',
                          'partially_filled'
                      )
                      AND position_id IS NULL
                    """
                )
                orphaned = dict(cursor.fetchone())

        return {
            "paper_client": True,
            "alpaca_account": account,
            "broker_orders": counts,
            "positions": positions,
            **orphaned,
            "database_writes_performed": False,
            "broker_orders_submitted": False,
            "broker_orders_cancelled": False,
        }

    def sync(self, limit):
        account = self.account_state()

        if account["account_blocked"]:
            return {
                "status": "blocked",
                "reason": "alpaca_account_blocked",
                "selected": 0,
                "results": [],
            }

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.validate_schema(cursor)
                rows = self.load_orders_to_sync(
                    cursor,
                    limit,
                )

            results = []

            for row in rows:
                try:
                    order = self.fetch_alpaca_order(row)

                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT *
                            FROM broker_orders
                            WHERE id = %s
                            FOR UPDATE
                            """,
                            (row["id"],),
                        )
                        locked = cursor.fetchone()

                        if locked is None:
                            connection.rollback()
                            continue

                        result = self.update_one(
                            cursor,
                            locked,
                            order,
                        )
                        connection.commit()

                    results.append(result)

                except Exception as error:
                    connection.rollback()
                    results.append(
                        {
                            "broker_order_id": str(row["id"]),
                            "client_order_id":
                                row["client_order_id"],
                            "symbol": row["symbol"],
                            "status": "sync_error",
                            "error": str(error),
                        }
                    )

            return {
                "status": "completed",
                "selected": len(rows),
                "results": results,
                "sync_errors": sum(
                    1
                    for result in results
                    if result.get("status") == "sync_error"
                ),
                "positions_created": sum(
                    1
                    for result in results
                    if result.get("position_created")
                ),
                "database_writes_performed": bool(rows),
                "broker_orders_submitted": False,
                "broker_orders_cancelled": False,
            }


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize DELTAX Alpaca PAPER orders and "
            "materialize positions from fills."
        )
    )

    mode = parser.add_mutually_exclusive_group(
        required=True
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Read-only reconciliation health check.",
    )
    mode.add_argument(
        "--sync",
        action="store_true",
        help=(
            "Synchronize existing broker orders. "
            "Never submits or cancels orders."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
    )

    args = parser.parse_args()

    if not 1 <= args.limit <= MAX_SYNC_LIMIT:
        parser.error(
            f"--limit must be between 1 and "
            f"{MAX_SYNC_LIMIT}"
        )

    return args


def main():
    args = parse_args()
    reconciler = BrokerOrderReconciler()

    result = (
        reconciler.health_check()
        if args.check
        else reconciler.sync(args.limit)
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=json_default,
        )
    )
    print("BROKER ORDER RECONCILER: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
