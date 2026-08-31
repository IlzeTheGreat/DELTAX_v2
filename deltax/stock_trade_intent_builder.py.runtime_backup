# File: deltax/stock_trade_intent_builder.py
# Purpose: Converts approved stock trade theses into audited, risk-gated
# stock entry intents. It NEVER submits broker orders.
#
# Source-of-truth logic:
# - ATR stop: 1.5 x ATR
# - ATR take-profit: 2.0 x ATR
# - planned max loss per stock trade <= $1,000
# - stock allocation cap <= $70,000
# - daily loss <= -3% blocks new entries
# - kill switch / bot controls fail closed
# - open same-strategy position, active cooldown, duplicate pending intent,
#   closed market, insufficient capital, or unshortable symbol blocks entry
# - IMPORTANT: this stock-only builder does NOT mark the thesis as
#   intents_created, because Core/Active may still need an options intent

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]
ALPACA_DATA_FEED = os.getenv("ALPACA_DATA_FEED_PAPER", "iex").lower()

EXPECTED_CONFIG_VERSION = "deltax_v2_strategy_v2"

MAX_STOCK_RISK_USD = Decimal("1000")
STOCK_ALLOCATION_CAP_USD = Decimal("70000")
DAILY_NEW_ENTRY_STOP_PCT = Decimal("-0.03")
DAILY_KILL_SWITCH_PCT = Decimal("-0.05")

STOP_ATR_MULTIPLIER = Decimal("1.5")
TAKE_PROFIT_ATR_MULTIPLIER = Decimal("2.0")

TRAILING_RULES = {
    "core": {
        "activation_pct": Decimal("0.08"),
        "distance_pct": Decimal("0.02"),
    },
    "active": {
        "activation_pct": Decimal("0.05"),
        "distance_pct": Decimal("0.02"),
    },
    "intraday": {
        "activation_pct": Decimal("0.012"),
        "distance_pct": Decimal("0.01"),
    },
}

ACTIVE_INTENT_STATUSES = (
    "created",
    "approved",
    "submitting",
    "submitted",
    "partially_filled",
)

MAX_PROCESS_LIMIT = 100


def json_default(value: Any):
    if isinstance(value, (datetime, date, Decimal)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    return str(value)


def decimal_value(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    return Decimal(str(value))


def normalize_daily_pct(value: Any) -> Decimal | None:
    """
    Accept either fractional form (-0.03) or percent-points form (-3.0).
    """
    if value is None:
        return None

    number = Decimal(str(value))

    if abs(number) > Decimal("1"):
        number = number / Decimal("100")

    return number


def floor_quantity(value: Decimal) -> Decimal:
    # Use whole shares for v1. This avoids fractional-short edge cases and
    # keeps broker behavior deterministic across long/short entries.
    return Decimal(math.floor(value))


def feed_enum():
    if ALPACA_DATA_FEED != "iex":
        raise RuntimeError(
            "DELTAX paper stock data feed must currently be 'iex', "
            f"found '{ALPACA_DATA_FEED}'"
        )
    return DataFeed.IEX


class StockTradeIntentBuilder:
    def __init__(self, database_url: str = DATABASE_URL):
        self.database_url = database_url
        self.trading_client = TradingClient(
            ALPACA_API_KEY,
            ALPACA_API_SECRET,
            paper=True,
        )
        self.data_client = StockHistoricalDataClient(
            ALPACA_API_KEY,
            ALPACA_API_SECRET,
        )

    def active_config(self, cursor):
        cursor.execute(
            """
            SELECT id, version, name, config
            FROM strategy_configs
            WHERE is_active = true
            ORDER BY activated_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()

        if row is None:
            raise RuntimeError("No active strategy configuration found")

        if row["version"] != EXPECTED_CONFIG_VERSION:
            raise RuntimeError(
                "Stock intent builder requires active config "
                f"{EXPECTED_CONFIG_VERSION}, found {row['version']}"
            )

        return row

    def bot_control(self, cursor):
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
            raise RuntimeError("bot_control row id=1 is missing")

        return row

    def pending_approved_theses(self, cursor, now, limit):
        cursor.execute(
            """
            SELECT theses.*
            FROM trade_theses theses
            WHERE theses.status = 'approved'
              AND theses.expires_at > %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM trade_intents intents
                  WHERE intents.trade_thesis_id = theses.id
                    AND intents.asset_class = 'stock'
                    AND intents.intent_type = 'entry'
              )
            ORDER BY theses.updated_at, theses.created_at
            LIMIT %s
            """,
            (now, limit),
        )
        return cursor.fetchall()

    def latest_prices(self, symbols):
        if not symbols:
            return {}

        request = StockLatestTradeRequest(
            symbol_or_symbols=sorted(set(symbols)),
            feed=feed_enum(),
        )
        response = self.data_client.get_stock_latest_trade(request)
        data = response.data if hasattr(response, "data") else response

        result = {}
        for symbol, trade in data.items():
            if trade is not None and trade.price is not None:
                result[symbol] = Decimal(str(trade.price))
        return result

    def account_state(self):
        account = self.trading_client.get_account()

        equity = decimal_value(getattr(account, "equity", None), Decimal("0"))
        last_equity = decimal_value(
            getattr(account, "last_equity", None),
            Decimal("0"),
        )
        buying_power = decimal_value(
            getattr(account, "buying_power", None),
            Decimal("0"),
        )

        if last_equity and last_equity > 0:
            daily_pct = (equity - last_equity) / last_equity
        else:
            daily_pct = None

        return {
            "equity": equity,
            "last_equity": last_equity,
            "buying_power": buying_power,
            "daily_pnl_pct": daily_pct,
            "account_status": str(getattr(account, "status", "")),
            "trading_blocked": bool(
                getattr(account, "trading_blocked", False)
            ),
            "account_blocked": bool(
                getattr(account, "account_blocked", False)
            ),
        }

    def latest_portfolio_state(self, cursor):
        cursor.execute(
            """
            SELECT
                captured_at,
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
                open_options_positions
            FROM portfolio_snapshots
            ORDER BY captured_at DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()

        if row is None:
            return None

        return dict(row)

    def current_stock_market_value(self, cursor, portfolio_state):
        if portfolio_state is not None:
            value = decimal_value(
                portfolio_state.get("stock_market_value"),
                Decimal("0"),
            )
            return abs(value or Decimal("0"))

        cursor.execute(
            """
            SELECT COALESCE(
                SUM(ABS(COALESCE(quantity, 0) * COALESCE(current_price, 0))),
                0
            ) AS stock_market_value
            FROM positions
            WHERE asset_class = 'stock'
              AND status IN ('opening', 'open', 'closing')
            """
        )
        return abs(decimal_value(cursor.fetchone()["stock_market_value"], Decimal("0")))

    def asset_gate(self, symbol, direction):
        if direction != "short":
            return [], {}

        try:
            asset = self.trading_client.get_asset(symbol)
        except Exception as exc:
            return ["alpaca_asset_lookup_failed"], {"error": str(exc)}

        details = {
            "shortable": bool(getattr(asset, "shortable", False)),
            "easy_to_borrow": bool(getattr(asset, "easy_to_borrow", False)),
            "tradable": bool(getattr(asset, "tradable", False)),
        }

        failures = []
        if details["tradable"] is not True:
            failures.append("symbol_not_tradable")
        if details["shortable"] is not True:
            failures.append("symbol_not_shortable")
        if details["easy_to_borrow"] is not True:
            failures.append("symbol_not_easy_to_borrow")

        return failures, details

    def thesis_gates(
        self,
        cursor,
        thesis,
        now,
        clock,
        bot_control,
        account_state,
        portfolio_state,
        current_price,
    ):
        failures = []
        details: dict[str, Any] = {}

        if bot_control["trading_mode"] != "paper":
            failures.append("trading_mode_not_paper")

        if bot_control["kill_switch_active"]:
            failures.append("kill_switch_active")

        if not bot_control["new_entries_enabled"]:
            failures.append("new_entries_disabled")

        if not bool(clock.is_open):
            failures.append("regular_market_not_open")

        if thesis["expires_at"] <= now:
            failures.append("thesis_expired")

        if account_state["account_blocked"]:
            failures.append("alpaca_account_blocked")

        if account_state["trading_blocked"]:
            failures.append("alpaca_trading_blocked")

        cursor.execute(
            """
            SELECT id
            FROM positions
            WHERE symbol = %s
              AND strategy = %s
              AND asset_class = 'stock'
              AND status IN ('opening', 'open', 'closing')
            LIMIT 1
            """,
            (thesis["symbol"], thesis["strategy"]),
        )
        open_position = cursor.fetchone()
        if open_position is not None:
            failures.append("same_strategy_stock_position_already_open")
            details["open_position_id"] = str(open_position["id"])

        cursor.execute(
            """
            SELECT reason, starts_at, ends_at
            FROM cooldowns
            WHERE symbol = %s
              AND strategy = %s
              AND direction = %s
              AND ends_at > %s
            LIMIT 1
            """,
            (
                thesis["symbol"],
                thesis["strategy"],
                thesis["direction"],
                now,
            ),
        )
        cooldown = cursor.fetchone()
        if cooldown is not None:
            failures.append("active_cooldown")
            details["cooldown"] = dict(cooldown)

        cursor.execute(
            """
            SELECT id, status
            FROM trade_intents
            WHERE symbol = %s
              AND strategy = %s
              AND asset_class = 'stock'
              AND intent_type = 'entry'
              AND status = ANY(%s)
            LIMIT 1
            """,
            (
                thesis["symbol"],
                thesis["strategy"],
                list(ACTIVE_INTENT_STATUSES),
            ),
        )
        pending_intent = cursor.fetchone()
        if pending_intent is not None:
            failures.append("duplicate_active_stock_entry_intent")
            details["active_intent_id"] = str(pending_intent["id"])
            details["active_intent_status"] = pending_intent["status"]

        daily_pct = None
        if portfolio_state is not None:
            daily_pct = normalize_daily_pct(
                portfolio_state.get("daily_pnl_pct")
            )

        if daily_pct is None:
            daily_pct = account_state["daily_pnl_pct"]

        details["daily_pnl_pct"] = daily_pct

        if daily_pct is not None and daily_pct <= DAILY_NEW_ENTRY_STOP_PCT:
            failures.append("daily_loss_limit_blocks_new_entries")

        if daily_pct is not None and daily_pct <= DAILY_KILL_SWITCH_PCT:
            failures.append("daily_kill_switch_threshold_reached")

        if current_price is None or current_price <= 0:
            failures.append("current_price_unavailable")

        atr = decimal_value(thesis.get("atr_14"))
        if atr is None or atr <= 0:
            failures.append("atr_14_missing_or_invalid")

        short_failures, asset_details = self.asset_gate(
            thesis["symbol"],
            thesis["direction"],
        )
        failures.extend(short_failures)
        if asset_details:
            details["asset"] = asset_details

        return list(dict.fromkeys(failures)), details

    def calculate_plan(
        self,
        thesis,
        current_price,
        account_state,
        stock_market_value,
    ):
        atr = decimal_value(thesis["atr_14"])
        if atr is None or atr <= 0:
            raise ValueError("ATR must be positive")

        entry = current_price
        stop_distance = STOP_ATR_MULTIPLIER * atr
        take_distance = TAKE_PROFIT_ATR_MULTIPLIER * atr

        if thesis["direction"] == "long":
            stop_loss = entry - stop_distance
            take_profit = entry + take_distance
            side = "buy"
        else:
            stop_loss = entry + stop_distance
            take_profit = entry - take_distance
            side = "sell"

        if stop_loss <= 0 or take_profit <= 0:
            raise ValueError("Calculated stop/take-profit price is invalid")

        qty_by_risk = MAX_STOCK_RISK_USD / stop_distance

        remaining_stock_capacity = max(
            Decimal("0"),
            STOCK_ALLOCATION_CAP_USD - stock_market_value,
        )
        usable_capital = min(
            remaining_stock_capacity,
            max(Decimal("0"), account_state["buying_power"]),
        )
        qty_by_capital = usable_capital / entry if entry > 0 else Decimal("0")

        quantity = floor_quantity(min(qty_by_risk, qty_by_capital))

        if quantity <= 0:
            raise ValueError("insufficient_capital_for_one_share")

        max_loss = quantity * stop_distance
        planned_notional = quantity * entry

        if max_loss > MAX_STOCK_RISK_USD:
            raise RuntimeError("Calculated max loss exceeds $1,000")

        trailing = TRAILING_RULES[thesis["strategy"]]
        if thesis["direction"] == "long":
            trailing_activation_price = (
                entry * (Decimal("1") + trailing["activation_pct"])
            )
        else:
            trailing_activation_price = (
                entry * (Decimal("1") - trailing["activation_pct"])
            )

        return {
            "side": side,
            "quantity": quantity,
            "entry_price": entry,
            "stop_loss_price": stop_loss,
            "take_profit_price": take_profit,
            "trailing_activation_price": trailing_activation_price,
            "trailing_distance_pct": trailing["distance_pct"],
            "max_loss": max_loss,
            "planned_notional": planned_notional,
            "remaining_stock_capacity_before": remaining_stock_capacity,
            "usable_capital_before": usable_capital,
            "atr_14": atr,
            "stop_distance": stop_distance,
            "take_profit_distance": take_distance,
        }

    def log_risk_failure(self, cursor, thesis, failures, details):
        severity = (
            "critical"
            if "daily_kill_switch_threshold_reached" in failures
            or "kill_switch_active" in failures
            else "warning"
        )

        cursor.execute(
            """
            INSERT INTO risk_events (
                severity,
                event_code,
                symbol,
                message,
                details
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                severity,
                "stock_entry_gate_rejected",
                thesis["symbol"],
                (
                    f"Stock entry blocked for {thesis['symbol']} "
                    f"{thesis['strategy']} {thesis['direction']}"
                ),
                Jsonb(
                    {
                        "trade_thesis_id": str(thesis["id"]),
                        "strategy": thesis["strategy"],
                        "direction": thesis["direction"],
                        "failures": failures,
                        "details": details,
                    }
                ),
            ),
        )

    def insert_intent(self, cursor, thesis, plan, now, gate_details):
        technical_state = thesis.get("technical_state") or {}
        market_state = thesis.get("market_state") or {}
        risk_state = thesis.get("risk_state") or {}

        idempotency_key = f"stock-entry:{thesis['id']}"

        metadata = {
            "builder_version": "deltax_stock_intent_v1_1",
            "trade_thesis_id": str(thesis["id"]),
            "scan_run_id": str(thesis["scan_run_id"]),
            "signal_at": thesis["signal_at"],
            "signal_price": thesis["signal_price"],
            "reference_vwap": thesis.get("reference_vwap"),
            "deviation_pct": thesis.get("deviation_pct"),
            "atr_14": thesis.get("atr_14"),
            "weak_indices_count": thesis.get("weak_indices_count"),
            "technical_state": technical_state,
            "market_state": market_state,
            "sector_state": thesis.get("sector_state") or {},
            "router_risk_state": risk_state,
            "risk_gates": {
                "status": "passed",
                "checked_at": now,
                "details": gate_details,
                "max_stock_risk_usd": MAX_STOCK_RISK_USD,
                "stock_allocation_cap_usd": STOCK_ALLOCATION_CAP_USD,
                "daily_new_entry_stop_pct": DAILY_NEW_ENTRY_STOP_PCT,
            },
            "planned_notional": plan["planned_notional"],
            "stop_atr_multiplier": STOP_ATR_MULTIPLIER,
            "take_profit_atr_multiplier": TAKE_PROFIT_ATR_MULTIPLIER,
        }

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
                trailing_activation_price,
                trailing_distance_pct,
                premium_type,
                net_premium,
                max_profit,
                max_loss,
                idempotency_key,
                status,
                expires_at,
                metadata
            )
            VALUES (
                %s, %s,
                'entry',
                'stock',
                %s, %s, %s, %s, %s,
                'market',
                'day',
                NULL,
                %s, %s, %s, %s, %s,
                'none',
                NULL,
                NULL,
                %s,
                %s,
                'approved',
                %s,
                %s
            )
            ON CONFLICT (idempotency_key)
            DO NOTHING
            RETURNING *
            """,
            (
                thesis["id"],
                thesis["strategy_config_id"],
                thesis["strategy"],
                thesis["direction"],
                thesis["symbol"],
                plan["side"],
                plan["quantity"],
                plan["entry_price"],
                plan["stop_loss_price"],
                plan["take_profit_price"],
                plan["trailing_activation_price"],
                plan["trailing_distance_pct"],
                plan["max_loss"],
                idempotency_key,
                thesis["expires_at"],
                Jsonb(metadata),
            ),
        )

        return cursor.fetchone()

    def health_check(self):
        now = datetime.now(timezone.utc)
        clock = self.trading_client.get_clock()
        account = self.account_state()

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                config = self.active_config(cursor)
                control = self.bot_control(cursor)
                portfolio = self.latest_portfolio_state(cursor)

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE status = 'approved'
                              AND expires_at > %s
                        ) AS approved_live_theses,
                        COUNT(*) FILTER (
                            WHERE status = 'intents_created'
                        ) AS theses_with_intents
                    FROM trade_theses
                    """,
                    (now,),
                )
                thesis_counts = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS stock_entry_intents,
                        COUNT(*) FILTER (
                            WHERE status = 'approved'
                        ) AS approved_stock_entry_intents
                    FROM trade_intents
                    WHERE asset_class = 'stock'
                      AND intent_type = 'entry'
                    """
                )
                intent_counts = cursor.fetchone()

        return {
            "config_version": config["version"],
            "bot_control": dict(control),
            "alpaca": {
                "paper_client": True,
                "market_open": bool(clock.is_open),
                "clock_timestamp": clock.timestamp,
                "account_status": account["account_status"],
                "trading_blocked": account["trading_blocked"],
                "account_blocked": account["account_blocked"],
                "equity": account["equity"],
                "buying_power": account["buying_power"],
                "daily_pnl_pct_from_account": account["daily_pnl_pct"],
            },
            "latest_portfolio_snapshot": portfolio,
            "theses": dict(thesis_counts),
            "intents": dict(intent_counts),
            "rules": {
                "max_stock_risk_usd": MAX_STOCK_RISK_USD,
                "stock_allocation_cap_usd": STOCK_ALLOCATION_CAP_USD,
                "daily_new_entry_stop_pct": DAILY_NEW_ENTRY_STOP_PCT,
                "stop_atr_multiplier": STOP_ATR_MULTIPLIER,
                "take_profit_atr_multiplier": TAKE_PROFIT_ATR_MULTIPLIER,
                "whole_share_v1": True,
            },
            "broker_orders_submitted": False,
            "writes_performed": False,
        }

    def process(self, limit):
        now = datetime.now(timezone.utc)
        clock = self.trading_client.get_clock()
        account = self.account_state()

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.active_config(cursor)
                control = self.bot_control(cursor)
                portfolio = self.latest_portfolio_state(cursor)
                theses = self.pending_approved_theses(
                    cursor,
                    now,
                    limit,
                )

            prices = self.latest_prices(
                [row["symbol"] for row in theses]
            )

            results = []

            for thesis in theses:
                with connection.cursor() as cursor:
                    # Serialize decisions for each thesis.
                    cursor.execute(
                        """
                        SELECT *
                        FROM trade_theses
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (thesis["id"],),
                    )
                    locked = cursor.fetchone()

                    if locked is None or locked["status"] != "approved":
                        results.append(
                            {
                                "thesis_id": str(thesis["id"]),
                                "symbol": thesis["symbol"],
                                "status": "skipped_not_approved_anymore",
                            }
                        )
                        connection.rollback()
                        continue

                    current_price = prices.get(locked["symbol"])

                    failures, gate_details = self.thesis_gates(
                        cursor=cursor,
                        thesis=locked,
                        now=now,
                        clock=clock,
                        bot_control=control,
                        account_state=account,
                        portfolio_state=portfolio,
                        current_price=current_price,
                    )

                    stock_market_value = self.current_stock_market_value(
                        cursor,
                        portfolio,
                    )
                    gate_details["stock_market_value"] = stock_market_value

                    plan = None
                    if not failures:
                        try:
                            plan = self.calculate_plan(
                                thesis=locked,
                                current_price=current_price,
                                account_state=account,
                                stock_market_value=stock_market_value,
                            )
                        except Exception as exc:
                            failures.append(str(exc))

                    if failures:
                        self.log_risk_failure(
                            cursor,
                            locked,
                            list(dict.fromkeys(failures)),
                            gate_details,
                        )
                        connection.commit()

                        results.append(
                            {
                                "thesis_id": str(locked["id"]),
                                "symbol": locked["symbol"],
                                "strategy": locked["strategy"],
                                "direction": locked["direction"],
                                "status": "blocked",
                                "failures": list(dict.fromkeys(failures)),
                            }
                        )
                        continue

                    intent = self.insert_intent(
                        cursor,
                        locked,
                        plan,
                        now,
                        gate_details,
                    )

                    if intent is None:
                        connection.rollback()
                        results.append(
                            {
                                "thesis_id": str(locked["id"]),
                                "symbol": locked["symbol"],
                                "status": "skipped_duplicate_intent",
                            }
                        )
                        continue

                    # Keep the thesis in 'approved' state here.
                    # A later intent coordinator may still create a Core/Active
                    # options intent from the same thesis. Only that coordinator
                    # should finally move the thesis to 'intents_created'.
                    connection.commit()

                    results.append(
                        {
                            "thesis_id": str(locked["id"]),
                            "intent_id": str(intent["id"]),
                            "symbol": locked["symbol"],
                            "strategy": locked["strategy"],
                            "direction": locked["direction"],
                            "status": "approved_intent_created",
                            "quantity": plan["quantity"],
                            "planned_entry_price": plan["entry_price"],
                            "stop_loss_price": plan["stop_loss_price"],
                            "take_profit_price": plan["take_profit_price"],
                            "max_loss": plan["max_loss"],
                        }
                    )

        return {
            "selected": len(theses),
            "results": results,
            "broker_orders_submitted": False,
        }


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "DELTAX stock trade-intent builder and deterministic entry risk gates."
        )
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Read-only health check. No database writes and no broker orders.",
    )
    mode.add_argument(
        "--process",
        action="store_true",
        help=(
            "Risk-check approved stock theses and create approved stock intents. "
            "Does not submit broker orders."
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=20,
    )

    args = parser.parse_args()

    if not 1 <= args.limit <= MAX_PROCESS_LIMIT:
        parser.error(
            f"--limit must be between 1 and {MAX_PROCESS_LIMIT}"
        )

    return args


def main():
    args = parse_args()
    builder = StockTradeIntentBuilder()

    result = (
        builder.health_check()
        if args.check
        else builder.process(args.limit)
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=json_default,
        )
    )
    print("STOCK TRADE INTENT BUILDER: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
