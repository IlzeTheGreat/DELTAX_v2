# File: deltax/portfolio_risk_monitor.py
# Purpose: Synchronize Alpaca PAPER account/positions into DELTAX portfolio
# snapshots and enforce deterministic portfolio-level risk gates.
#
# Rules:
#   daily PnL <= -3%  -> block NEW entries
#   daily PnL <= -5%  -> activate kill switch + disable execution/new entries
#
# Safety:
# - PAPER account only.
# - --check is read-only.
# - --sync never submits/cancels broker orders.
# - This module does NOT yet create emergency exit intents. That is handled by
#   the next exit-intent module. The -5% state is persisted so execution cannot
#   open further risk while exits are being prepared.

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
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

NEW_ENTRY_STOP_PCT = Decimal("-0.03")
KILL_SWITCH_PCT = Decimal("-0.05")


def D(value, default=Decimal("0")):
    if value is None:
        return default
    return Decimal(str(value))


def json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def model_payload(value):
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return {"repr": str(value)}


class PortfolioRiskMonitor:
    def __init__(self):
        self.client = TradingClient(
            ALPACA_API_KEY,
            ALPACA_API_SECRET,
            paper=True,
        )

    def account_state(self):
        account = self.client.get_account()

        equity = D(getattr(account, "equity", 0))
        last_equity = D(getattr(account, "last_equity", 0))
        cash = D(getattr(account, "cash", 0))
        buying_power = D(getattr(account, "buying_power", 0))

        if last_equity > 0:
            daily_pnl = equity - last_equity
            daily_pnl_pct = daily_pnl / last_equity
        else:
            daily_pnl = Decimal("0")
            daily_pnl_pct = Decimal("0")

        return {
            "status": str(getattr(account, "status", "")),
            "account_blocked": bool(
                getattr(account, "account_blocked", False)
            ),
            "trading_blocked": bool(
                getattr(account, "trading_blocked", False)
            ),
            "equity": equity,
            "last_equity": last_equity,
            "cash": cash,
            "buying_power": buying_power,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
            "raw": model_payload(account),
        }

    def alpaca_positions(self):
        result = []

        for position in self.client.get_all_positions():
            asset_class = str(
                getattr(position, "asset_class", "")
            ).lower()

            result.append(
                {
                    "symbol": getattr(position, "symbol", None),
                    "asset_class": asset_class,
                    "qty": D(getattr(position, "qty", 0)),
                    "market_value": D(
                        getattr(position, "market_value", 0)
                    ),
                    "avg_entry_price": D(
                        getattr(position, "avg_entry_price", 0)
                    ),
                    "current_price": D(
                        getattr(position, "current_price", 0)
                    ),
                    "unrealized_pl": D(
                        getattr(position, "unrealized_pl", 0)
                    ),
                    "unrealized_plpc": D(
                        getattr(position, "unrealized_plpc", 0)
                    ),
                    "raw": model_payload(position),
                }
            )

        return result

    def validate_schema(self, cursor):
        required = {
            "portfolio_snapshots": {
                "equity",
                "cash",
                "buying_power",
                "stock_market_value",
                "options_market_value",
                "stock_open_risk",
                "options_open_risk",
                "daily_pnl",
                "daily_pnl_pct",
                "open_stock_positions",
                "open_options_positions",
                "raw_payload",
            },
            "bot_control": {
                "id",
                "execution_enabled",
                "new_entries_enabled",
                "kill_switch_active",
                "kill_switch_reason",
            },
            "risk_events": {
                "severity",
                "event_code",
                "message",
                "details",
                "occurred_at",
                "resolved_at",
            },
            "positions": {
                "id",
                "symbol",
                "asset_class",
                "status",
                "quantity",
                "current_price",
                "unrealized_pnl",
                "initial_max_loss",
            },
            "position_snapshots": {
                "position_id",
                "current_price",
                "market_value",
                "unrealized_pnl",
                "unrealized_pnl_pct",
                "stop_loss_price",
                "take_profit_price",
                "trailing_stop_price",
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
            actual.setdefault(
                row["table_name"], set()
            ).add(row["column_name"])

        missing = {
            table: sorted(columns - actual.get(table, set()))
            for table, columns in required.items()
            if columns - actual.get(table, set())
        }

        if missing:
            raise RuntimeError(
                f"Required portfolio/risk schema missing: {missing}"
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
            raise RuntimeError("bot_control row id=1 missing")
        return row

    def db_position_summary(self, cursor):
        cursor.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE status IN ('opening', 'open', 'closing')
                      AND asset_class = 'stock'
                ) AS open_stock_positions,
                COUNT(*) FILTER (
                    WHERE status IN ('opening', 'open', 'closing')
                      AND asset_class = 'option_spread'
                ) AS open_options_positions,
                COALESCE(SUM(
                    CASE
                        WHEN status IN ('opening','open','closing')
                         AND asset_class = 'stock'
                        THEN ABS(COALESCE(initial_max_loss, 0))
                        ELSE 0
                    END
                ), 0) AS stock_open_risk,
                COALESCE(SUM(
                    CASE
                        WHEN status IN ('opening','open','closing')
                         AND asset_class = 'option_spread'
                        THEN ABS(COALESCE(initial_max_loss, 0))
                        ELSE 0
                    END
                ), 0) AS options_open_risk
            FROM positions
            """
        )
        return dict(cursor.fetchone())

    def update_internal_positions(
        self,
        cursor,
        alpaca_positions,
    ):
        # Stocks can be mapped directly by symbol. Option spreads cannot be
        # safely reconstructed from individual Alpaca option legs here, so
        # their DELTAX spread state remains reconciler/exit-monitor territory.
        stock_map = {
            item["symbol"]: item
            for item in alpaca_positions
            if "us_equity" in item["asset_class"]
            or item["asset_class"].endswith("stock")
        }

        updated = 0

        cursor.execute(
            """
            SELECT id, symbol
            FROM positions
            WHERE asset_class = 'stock'
              AND status IN ('opening', 'open', 'closing')
            """
        )

        for row in cursor.fetchall():
            broker_position = stock_map.get(row["symbol"])
            if broker_position is None:
                continue

            cursor.execute(
                """
                UPDATE positions
                SET
                    quantity = %s,
                    current_price = %s,
                    unrealized_pnl = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    abs(broker_position["qty"]),
                    broker_position["current_price"],
                    broker_position["unrealized_pl"],
                    row["id"],
                ),
            )
            updated += cursor.rowcount

            cursor.execute(
                """
                INSERT INTO position_snapshots (
                    position_id,
                    current_price,
                    market_value,
                    unrealized_pnl,
                    unrealized_pnl_pct,
                    stop_loss_price,
                    take_profit_price,
                    trailing_stop_price,
                    raw_payload
                )
                SELECT
                    positions.id,
                    %s,
                    %s,
                    %s,
                    %s,
                    positions.stop_loss_price,
                    positions.take_profit_price,
                    positions.trailing_stop_price,
                    %s
                FROM positions
                WHERE positions.id = %s
                """,
                (
                    broker_position["current_price"],
                    broker_position["market_value"],
                    broker_position["unrealized_pl"],
                    broker_position["unrealized_plpc"],
                    Jsonb(broker_position["raw"]),
                    row["id"],
                ),
            )

        return updated

    def ensure_risk_event(
        self,
        cursor,
        *,
        severity,
        event_code,
        message,
        details,
    ):
        # Avoid one duplicate event every five minutes for the same unresolved
        # portfolio-level condition.
        cursor.execute(
            """
            SELECT id
            FROM risk_events
            WHERE event_code = %s
              AND resolved_at IS NULL
            ORDER BY occurred_at DESC
            LIMIT 1
            """,
            (event_code,),
        )

        if cursor.fetchone() is not None:
            return False

        cursor.execute(
            """
            INSERT INTO risk_events (
                severity,
                event_code,
                message,
                details
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                severity,
                event_code,
                message,
                Jsonb(details),
            ),
        )
        return True

    def resolve_event(self, cursor, event_code):
        cursor.execute(
            """
            UPDATE risk_events
            SET resolved_at = now()
            WHERE event_code = %s
              AND resolved_at IS NULL
            """,
            (event_code,),
        )
        return cursor.rowcount

    def apply_controls(
        self,
        cursor,
        account,
        current_control,
    ):
        daily_pct = account["daily_pnl_pct"]
        changes = []
        events = []

        if daily_pct <= KILL_SWITCH_PCT:
            cursor.execute(
                """
                UPDATE bot_control
                SET
                    kill_switch_active = true,
                    kill_switch_reason = %s,
                    new_entries_enabled = false,
                    execution_enabled = false,
                    updated_at = now()
                WHERE id = 1
                """,
                (
                    "daily_loss_limit_-5pct",
                ),
            )

            changes.append(
                "kill_switch_active=true"
            )
            changes.append(
                "new_entries_enabled=false"
            )
            changes.append(
                "execution_enabled=false"
            )

            created = self.ensure_risk_event(
                cursor,
                severity="critical",
                event_code="daily_loss_kill_switch",
                message=(
                    "Daily portfolio loss reached the -5% "
                    "kill-switch threshold."
                ),
                details={
                    "daily_pnl_pct": str(daily_pct),
                    "threshold": str(KILL_SWITCH_PCT),
                    "equity": str(account["equity"]),
                    "last_equity": str(account["last_equity"]),
                },
            )
            events.append(
                {
                    "event_code": "daily_loss_kill_switch",
                    "created": created,
                }
            )

        elif daily_pct <= NEW_ENTRY_STOP_PCT:
            cursor.execute(
                """
                UPDATE bot_control
                SET
                    new_entries_enabled = false,
                    updated_at = now()
                WHERE id = 1
                """
            )

            changes.append(
                "new_entries_enabled=false"
            )

            created = self.ensure_risk_event(
                cursor,
                severity="warning",
                event_code="daily_loss_new_entries_blocked",
                message=(
                    "Daily portfolio loss reached the -3% "
                    "new-entry stop threshold."
                ),
                details={
                    "daily_pnl_pct": str(daily_pct),
                    "threshold": str(NEW_ENTRY_STOP_PCT),
                    "equity": str(account["equity"]),
                    "last_equity": str(account["last_equity"]),
                },
            )
            events.append(
                {
                    "event_code":
                        "daily_loss_new_entries_blocked",
                    "created": created,
                }
            )

        else:
            # We deliberately DO NOT automatically re-enable entries or reset
            # the kill switch. Re-arming risk after a breach requires an
            # explicit operator action.
            self.resolve_event(
                cursor,
                "daily_loss_new_entries_blocked",
            )

        return {
            "changes": changes,
            "events": events,
        }

    def snapshot_values(
        self,
        account,
        alpaca_positions,
        db_summary,
    ):
        stock_market_value = Decimal("0")
        options_market_value = Decimal("0")

        for item in alpaca_positions:
            if "option" in item["asset_class"]:
                options_market_value += abs(
                    item["market_value"]
                )
            else:
                stock_market_value += abs(
                    item["market_value"]
                )

        return {
            "equity": account["equity"],
            "cash": account["cash"],
            "buying_power": account["buying_power"],
            "stock_market_value": stock_market_value,
            "options_market_value": options_market_value,
            "stock_open_risk": D(
                db_summary["stock_open_risk"]
            ),
            "options_open_risk": D(
                db_summary["options_open_risk"]
            ),
            "daily_pnl": account["daily_pnl"],
            "daily_pnl_pct": account["daily_pnl_pct"],
            "open_stock_positions": int(
                db_summary["open_stock_positions"]
            ),
            "open_options_positions": int(
                db_summary["open_options_positions"]
            ),
        }

    def health_check(self):
        account = self.account_state()
        alpaca_positions = self.alpaca_positions()

        with psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.validate_schema(cursor)
                control = dict(self.load_control(cursor))
                db_summary = self.db_position_summary(
                    cursor
                )

                cursor.execute(
                    """
                    SELECT *
                    FROM portfolio_snapshots
                    ORDER BY captured_at DESC
                    LIMIT 1
                    """
                )
                latest = cursor.fetchone()

        return {
            "paper_client": True,
            "account": {
                key: value
                for key, value in account.items()
                if key != "raw"
            },
            "risk_thresholds": {
                "block_new_entries": NEW_ENTRY_STOP_PCT,
                "kill_switch": KILL_SWITCH_PCT,
            },
            "bot_control": control,
            "alpaca_open_positions":
                len(alpaca_positions),
            "database_position_summary":
                db_summary,
            "latest_portfolio_snapshot":
                dict(latest) if latest else None,
            "database_writes_performed": False,
            "broker_orders_submitted": False,
            "broker_orders_cancelled": False,
        }

    def sync(self):
        account = self.account_state()
        alpaca_positions = self.alpaca_positions()

        with psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.validate_schema(cursor)
                control_before = dict(
                    self.load_control(cursor)
                )
                db_summary = self.db_position_summary(
                    cursor
                )

                internal_stock_positions_updated = (
                    self.update_internal_positions(
                        cursor,
                        alpaca_positions,
                    )
                )

                # Re-read after stock position sync.
                db_summary = self.db_position_summary(
                    cursor
                )

                values = self.snapshot_values(
                    account,
                    alpaca_positions,
                    db_summary,
                )

                cursor.execute(
                    """
                    INSERT INTO portfolio_snapshots (
                        equity,
                        cash,
                        buying_power,
                        stock_market_value,
                        options_market_value,
                        stock_open_risk,
                        options_open_risk,
                        daily_pnl,
                        daily_pnl_pct,
                        open_stock_positions,
                        open_options_positions,
                        raw_payload
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    RETURNING id, captured_at
                    """,
                    (
                        values["equity"],
                        values["cash"],
                        values["buying_power"],
                        values["stock_market_value"],
                        values["options_market_value"],
                        values["stock_open_risk"],
                        values["options_open_risk"],
                        values["daily_pnl"],
                        values["daily_pnl_pct"],
                        values["open_stock_positions"],
                        values["open_options_positions"],
                        Jsonb(
                            {
                                "alpaca_account":
                                    account["raw"],
                                "alpaca_positions": [
                                    item["raw"]
                                    for item in
                                    alpaca_positions
                                ],
                            }
                        ),
                    ),
                )
                snapshot = dict(cursor.fetchone())

                risk = self.apply_controls(
                    cursor,
                    account,
                    control_before,
                )

                control_after = dict(
                    self.load_control(cursor)
                )

            connection.commit()

        return {
            "status": "completed",
            "portfolio_snapshot_id":
                snapshot["id"],
            "captured_at":
                snapshot["captured_at"],
            "portfolio": values,
            "internal_stock_positions_updated":
                internal_stock_positions_updated,
            "bot_control_before":
                control_before,
            "bot_control_after":
                control_after,
            "risk_actions": risk,
            "database_writes_performed": True,
            "broker_orders_submitted": False,
            "broker_orders_cancelled": False,
            "emergency_exit_intents_created": False,
        }


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Persist DELTAX portfolio state and enforce "
            "daily portfolio risk gates."
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
        "--sync",
        action="store_true",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    monitor = PortfolioRiskMonitor()

    result = (
        monitor.health_check()
        if args.check
        else monitor.sync()
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=json_default,
        )
    )
    print("PORTFOLIO RISK MONITOR: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
