# File: deltax/options_spread_intent_builder.py
# Purpose: Converts eligible approved Core/Active trade theses into
# defined-risk Alpaca option-spread entry intents.
#
# IMPORTANT:
# - NEVER submits broker orders.
# - Uses only approved Core/Active theses with a fresh, material AI catalyst.
# - Bullish thesis -> bull put credit spread.
# - Bearish thesis -> bear call credit spread.
# - 7-21 DTE, short-leg |delta| 0.20-0.30.
# - Minimum net credit = 30% of strike width.
# - Max planned loss <= $1,000 per option position.
# - Max 5 open option positions and <= $5,000 total option max loss.
# - No new option position in the last 30 minutes of regular session.
#
# NOTE ON LIQUIDITY:
# Alpaca's option-chain snapshot endpoint reliably provides bid/ask, IV and
# Greeks. Open-interest is not guaranteed in this response. V1 therefore
# enforces executable bid/ask quotes and quote-width sanity as a liquidity
# proxy. The missing OI/volume check is explicitly recorded in metadata.

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]

EXPECTED_CONFIG_VERSION = "deltax_v2_strategy_v2"

MIN_DTE = 7
MAX_DTE = 21
SHORT_DELTA_MIN = Decimal("0.20")
SHORT_DELTA_MAX = Decimal("0.30")
TARGET_SHORT_DELTA = Decimal("0.25")
MIN_CREDIT_TO_WIDTH = Decimal("0.30")

MAX_OPTION_TRADE_LOSS = Decimal("1000")
MAX_OPTION_PORTFOLIO_LOSS = Decimal("5000")
MAX_OPEN_OPTION_POSITIONS = 5
MAX_OPTION_POSITIONS_PER_SYMBOL = 1
MAX_COMBINED_IDEAS_PER_SECTOR = 2

NO_NEW_ENTRY_MINUTES = 30
MIN_AI_CONFIDENCE = Decimal("0.65")

# Engineering guardrail. The strategy requires that bid/ask not be
# "excessively wide" but does not define a numeric threshold.
# V1 uses 35% of mid as a conservative executable-quote sanity check.
MAX_LEG_BID_ASK_PCT_OF_MID = Decimal("0.35")

ACTIVE_POSITION_STATUSES = ("opening", "open", "closing")
ACTIVE_INTENT_STATUSES = (
    "created",
    "approved",
    "submitting",
    "submitted",
    "partially_filled",
)

MAX_PROCESS_LIMIT = 50


def json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def D(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    return Decimal(str(value))


def response_data(response):
    return response.data if hasattr(response, "data") else response


def quote_values(snapshot):
    quote = getattr(snapshot, "latest_quote", None)
    if quote is None:
        return None, None, None

    bid = D(getattr(quote, "bid_price", None))
    ask = D(getattr(quote, "ask_price", None))

    if bid is None or ask is None:
        return bid, ask, None

    mid = (bid + ask) / Decimal("2")
    return bid, ask, mid


def greek_delta(snapshot):
    greeks = getattr(snapshot, "greeks", None)
    if greeks is None:
        return None
    return D(getattr(greeks, "delta", None))


def implied_volatility(snapshot):
    return D(getattr(snapshot, "implied_volatility", None))


def parse_occ_symbol(contract_symbol: str):
    """
    OCC-style symbols end with:
      YYMMDD + C/P + 8-digit strike (1/1000 dollars)
    Example: AAPL260909P00320000
    """
    if len(contract_symbol) < 15:
        return None

    suffix = contract_symbol[-15:]
    date_part = suffix[:6]
    option_code = suffix[6]
    strike_part = suffix[7:]

    if option_code not in {"C", "P"}:
        return None

    try:
        expiration = datetime.strptime(date_part, "%y%m%d").date()
        strike = Decimal(int(strike_part)) / Decimal("1000")
    except Exception:
        return None

    return {
        "expiration_date": expiration,
        "option_type": "call" if option_code == "C" else "put",
        "strike": strike,
    }


def leg_quote_ok(bid: Decimal, ask: Decimal, mid: Decimal):
    if bid is None or ask is None or mid is None:
        return False
    if bid <= 0 or ask <= 0 or ask < bid or mid <= 0:
        return False

    spread = ask - bid
    return (spread / mid) <= MAX_LEG_BID_ASK_PCT_OF_MID


class OptionsSpreadIntentBuilder:
    def __init__(self, database_url=DATABASE_URL):
        self.database_url = database_url
        self.trading_client = TradingClient(
            ALPACA_API_KEY,
            ALPACA_API_SECRET,
            paper=True,
        )
        self.option_client = OptionHistoricalDataClient(
            ALPACA_API_KEY,
            ALPACA_API_SECRET,
        )

    def load_active_config(self, cursor):
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
                "Options builder requires active config "
                f"{EXPECTED_CONFIG_VERSION}, found {row['version']}"
            )

        return row

    def load_bot_control(self, cursor):
        cursor.execute(
            """
            SELECT
                trading_mode,
                execution_enabled,
                new_entries_enabled,
                kill_switch_active,
                kill_switch_reason
            FROM bot_control
            WHERE id = 1
            """
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("bot_control row id=1 is missing")
        return row

    def load_pending_theses(self, cursor, now, limit):
        cursor.execute(
            """
            SELECT
                theses.*,
                analyses.direction AS ai_direction,
                analyses.confidence AS ai_confidence,
                analyses.raw_response AS ai_raw_response,
                instruments.sector
            FROM trade_theses theses
            LEFT JOIN ai_analyses analyses
              ON analyses.id = theses.ai_analysis_id
            JOIN instruments
              ON instruments.symbol = theses.symbol
            WHERE theses.status = 'approved'
              AND theses.strategy IN ('core', 'active')
              AND theses.expires_at > %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM trade_intents intents
                  WHERE intents.trade_thesis_id = theses.id
                    AND intents.asset_class = 'option_spread'
                    AND intents.intent_type = 'entry'
              )
            ORDER BY theses.updated_at, theses.created_at
            LIMIT %s
            """,
            (now, limit),
        )
        return cursor.fetchall()

    def next_regular_session_date(self, now):
        start = now.date()
        end = start + timedelta(days=10)

        sessions = self.trading_client.get_calendar(
            GetCalendarRequest(start=start, end=end)
        )

        for session in sessions:
            session_date = session.date
            if session_date > start:
                return session_date

        return None

    def minutes_to_close(self, clock):
        close = clock.next_close
        current = clock.timestamp

        if close.tzinfo is None:
            close = close.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)

        return (close - current).total_seconds() / 60.0

    def portfolio_state(self, cursor):
        cursor.execute(
            """
            SELECT
                COUNT(*) AS open_option_positions,
                COALESCE(SUM(initial_max_loss), 0) AS option_open_risk
            FROM positions
            WHERE asset_class = 'option_spread'
              AND status = ANY(%s)
            """,
            (list(ACTIVE_POSITION_STATUSES),),
        )
        return cursor.fetchone()

    def current_symbol_option_positions(self, cursor, symbol):
        cursor.execute(
            """
            SELECT COUNT(*) AS count
            FROM positions
            WHERE asset_class = 'option_spread'
              AND symbol = %s
              AND status = ANY(%s)
            """,
            (symbol, list(ACTIVE_POSITION_STATUSES)),
        )
        return int(cursor.fetchone()["count"])

    def sector_idea_count(self, cursor, sector):
        if not sector:
            return 0

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM positions positions_data
            JOIN instruments
              ON instruments.symbol = positions_data.symbol
            WHERE instruments.sector = %s
              AND positions_data.status = ANY(%s)
              AND positions_data.asset_class IN ('stock', 'option_spread')
            """,
            (sector, list(ACTIVE_POSITION_STATUSES)),
        )
        return int(cursor.fetchone()[0])

    def has_pending_option_intent_for_symbol(self, cursor, symbol):
        cursor.execute(
            """
            SELECT id
            FROM trade_intents
            WHERE asset_class = 'option_spread'
              AND intent_type = 'entry'
              AND symbol = %s
              AND status = ANY(%s)
            LIMIT 1
            """,
            (symbol, list(ACTIVE_INTENT_STATUSES)),
        )
        return cursor.fetchone()

    def earnings_gate(self, cursor, symbol, now):
        next_session = self.next_regular_session_date(now)

        if next_session is None:
            return ["next_regular_session_unavailable"], {}

        cursor.execute(
            """
            SELECT
                report_date,
                report_time,
                status,
                source
            FROM earnings_events
            WHERE symbol = %s
              AND status = 'scheduled'
              AND report_date >= %s
              AND report_date <= %s
            ORDER BY report_date
            LIMIT 1
            """,
            (symbol, now.date(), next_session),
        )
        row = cursor.fetchone()

        if row is None:
            return [], {"next_session_date": next_session}

        return (
            ["earnings_due_by_next_full_trading_day"],
            {
                "next_session_date": next_session,
                "earnings": dict(row),
            },
        )

    def ai_gate(self, thesis):
        failures = []
        raw = thesis.get("ai_raw_response")
        if not isinstance(raw, dict):
            raw = {}

        confidence = D(thesis.get("ai_confidence"))

        if thesis.get("ai_analysis_id") is None:
            failures.append("options_require_ai_analysis")

        if thesis.get("ai_direction") not in {"bullish", "bearish"}:
            failures.append("options_require_directional_ai_analysis")

        expected_ai_direction = (
            "bullish" if thesis["direction"] == "long" else "bearish"
        )
        if thesis.get("ai_direction") != expected_ai_direction:
            failures.append("ai_direction_does_not_match_trade_thesis")

        if confidence is None or confidence < MIN_AI_CONFIDENCE:
            failures.append("ai_confidence_below_0_65")

        if raw.get("meaningful_company_specific_catalyst") is not True:
            failures.append("no_meaningful_company_specific_catalyst")

        if raw.get("sufficient_news") is not True:
            failures.append("insufficient_news")

        return failures, {
            "ai_analysis_id": str(thesis["ai_analysis_id"])
            if thesis.get("ai_analysis_id")
            else None,
            "ai_direction": thesis.get("ai_direction"),
            "ai_confidence": confidence,
            "meaningful_company_specific_catalyst": raw.get(
                "meaningful_company_specific_catalyst"
            ),
            "sufficient_news": raw.get("sufficient_news"),
        }

    def fetch_chain(self, symbol, now):
        request = OptionChainRequest(
            underlying_symbol=symbol,
            expiration_date_gte=now.date() + timedelta(days=MIN_DTE),
            expiration_date_lte=now.date() + timedelta(days=MAX_DTE),
        )
        response = self.option_client.get_option_chain(request)
        chain = response_data(response)
        return dict(chain.items()) if hasattr(chain, "items") else {}

    def normalized_contracts(self, chain, now):
        contracts = []

        for contract_symbol, snapshot in chain.items():
            parsed = parse_occ_symbol(contract_symbol)
            if parsed is None:
                continue

            dte = (parsed["expiration_date"] - now.date()).days
            if dte < MIN_DTE or dte > MAX_DTE:
                continue

            bid, ask, mid = quote_values(snapshot)
            delta = greek_delta(snapshot)
            iv = implied_volatility(snapshot)

            contracts.append(
                {
                    "contract_symbol": contract_symbol,
                    "snapshot": snapshot,
                    "option_type": parsed["option_type"],
                    "expiration_date": parsed["expiration_date"],
                    "dte": dte,
                    "strike": parsed["strike"],
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "delta": delta,
                    "iv": iv,
                }
            )

        return contracts

    def candidate_spreads(self, thesis, contracts):
        option_type = "put" if thesis["direction"] == "long" else "call"

        usable = [
            item
            for item in contracts
            if item["option_type"] == option_type
            and item["delta"] is not None
            and item["iv"] is not None
            and item["bid"] is not None
            and item["ask"] is not None
            and item["mid"] is not None
            and leg_quote_ok(item["bid"], item["ask"], item["mid"])
        ]

        short_legs = []
        for item in usable:
            abs_delta = abs(item["delta"])
            if SHORT_DELTA_MIN <= abs_delta <= SHORT_DELTA_MAX:
                short_legs.append(item)

        by_expiration = {}
        for item in usable:
            by_expiration.setdefault(item["expiration_date"], []).append(item)

        candidates = []

        for short_leg in short_legs:
            same_expiry = by_expiration.get(short_leg["expiration_date"], [])

            if thesis["direction"] == "long":
                protective = [
                    item for item in same_expiry
                    if item["strike"] < short_leg["strike"]
                ]
                protective.sort(
                    key=lambda item: item["strike"],
                    reverse=True,
                )
            else:
                protective = [
                    item for item in same_expiry
                    if item["strike"] > short_leg["strike"]
                ]
                protective.sort(
                    key=lambda item: item["strike"],
                )

            for long_leg in protective:
                width = abs(short_leg["strike"] - long_leg["strike"])
                if width <= 0:
                    continue

                # Conservative executable credit estimate:
                # sell short leg at bid, buy protective leg at ask.
                credit = short_leg["bid"] - long_leg["ask"]
                if credit <= 0:
                    continue

                ratio = credit / width
                if ratio < MIN_CREDIT_TO_WIDTH:
                    continue

                max_loss_one = (width - credit) * Decimal("100")
                max_profit_one = credit * Decimal("100")

                if max_loss_one <= 0:
                    continue

                contracts_count = int(
                    math.floor(
                        float(MAX_OPTION_TRADE_LOSS / max_loss_one)
                    )
                )

                if contracts_count < 1:
                    continue

                candidates.append(
                    {
                        "short_leg": short_leg,
                        "long_leg": long_leg,
                        "width": width,
                        "credit": credit,
                        "credit_to_width": ratio,
                        "max_loss_per_contract": max_loss_one,
                        "max_profit_per_contract": max_profit_one,
                        "contracts": contracts_count,
                        "max_loss": max_loss_one * contracts_count,
                        "max_profit": max_profit_one * contracts_count,
                        "delta_distance": abs(
                            abs(short_leg["delta"]) - TARGET_SHORT_DELTA
                        ),
                    }
                )

        candidates.sort(
            key=lambda item: (
                item["delta_distance"],
                -item["credit_to_width"],
                item["short_leg"]["dte"],
            )
        )
        return candidates

    def persist_quote_snapshot(self, cursor, thesis, contract):
        cursor.execute(
            """
            INSERT INTO option_quote_snapshots (
                scan_run_id,
                trade_thesis_id,
                underlying_symbol,
                contract_symbol,
                option_type,
                expiration_date,
                dte,
                strike,
                multiplier,
                bid_price,
                ask_price,
                mid_price,
                last_price,
                volume,
                open_interest,
                implied_volatility,
                delta,
                gamma,
                theta,
                vega,
                rho,
                quote_timestamp,
                feed,
                raw_payload
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                100,
                %s, %s, %s,
                NULL,
                NULL,
                NULL,
                %s, %s, NULL, NULL, NULL, NULL,
                NULL,
                'alpaca_option_chain',
                %s
            )
            RETURNING id
            """,
            (
                thesis["scan_run_id"],
                thesis["id"],
                thesis["symbol"],
                contract["contract_symbol"],
                contract["option_type"],
                contract["expiration_date"],
                contract["dte"],
                contract["strike"],
                contract["bid"],
                contract["ask"],
                contract["mid"],
                contract["iv"],
                contract["delta"],
                Jsonb(
                    {
                        "liquidity_check": "bid_ask_proxy_v1",
                        "volume_available": False,
                        "open_interest_available": False,
                    }
                ),
            ),
        )
        return cursor.fetchone()["id"]

    def create_option_intent(
        self,
        cursor,
        thesis,
        spread,
        gate_details,
        now,
    ):
        short_leg = spread["short_leg"]
        long_leg = spread["long_leg"]

        short_snapshot_id = self.persist_quote_snapshot(
            cursor, thesis, short_leg
        )
        long_snapshot_id = self.persist_quote_snapshot(
            cursor, thesis, long_leg
        )

        idempotency_key = f"option-spread-entry:{thesis['id']}"

        net_credit_total = (
            spread["credit"]
            * Decimal("100")
            * spread["contracts"]
        )

        metadata = {
            "builder_version": "deltax_options_spread_intent_v1_1",
            "strategy_name": "AI-Directed Defined-Risk Premium Strategy",
            "spread_type": (
                "bull_put_credit_spread"
                if thesis["direction"] == "long"
                else "bear_call_credit_spread"
            ),
            "checked_at": now,
            "gate_details": gate_details,
            "pricing_semantics": {
                "alpaca_mleg_limit_price": -spread["credit"],
                "limit_price_meaning": "negative value = credit per spread unit",
                "net_credit_total_usd": net_credit_total,
            },
            "selection": {
                "dte": short_leg["dte"],
                "short_delta": short_leg["delta"],
                "short_iv": short_leg["iv"],
                "strike_width": spread["width"],
                "credit_per_share": spread["credit"],
                "credit_to_width": spread["credit_to_width"],
                "contracts": spread["contracts"],
            },
            "liquidity_note": (
                "V1 enforces live bid/ask and quote-width sanity. "
                "Alpaca option-chain snapshot did not expose guaranteed "
                "volume/open-interest fields, so OI/volume are not yet a hard gate."
            ),
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
                'option_spread',
                %s, %s, %s,
                NULL,
                %s,
                'limit',
                'day',
                %s,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                'credit',
                %s,
                %s,
                %s,
                %s,
                'approved',
                %s,
                %s
            )
            ON CONFLICT (idempotency_key)
            DO NOTHING
            RETURNING id
            """,
            (
                thesis["id"],
                thesis["strategy_config_id"],
                thesis["strategy"],
                thesis["direction"],
                thesis["symbol"],
                spread["contracts"],
                -spread["credit"],
                net_credit_total,
                spread["max_profit"],
                spread["max_loss"],
                idempotency_key,
                thesis["expires_at"],
                Jsonb(metadata),
            ),
        )

        row = cursor.fetchone()
        if row is None:
            return None

        intent_id = row["id"]

        short_action = "sell_to_open"
        long_action = "buy_to_open"

        for leg_number, action, contract, snapshot_id in (
            (1, short_action, short_leg, short_snapshot_id),
            (2, long_action, long_leg, long_snapshot_id),
        ):
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
                    %s, %s, %s, %s, %s,
                    1, %s, %s, %s, 100,
                    %s, %s, %s
                )
                """,
                (
                    intent_id,
                    leg_number,
                    snapshot_id,
                    contract["contract_symbol"],
                    action,
                    contract["option_type"],
                    contract["strike"],
                    contract["expiration_date"],
                    contract["bid"],
                    contract["ask"],
                    contract["mid"],
                ),
            )

        return intent_id

    def log_block(self, cursor, thesis, failures, details):
        cursor.execute(
            """
            INSERT INTO risk_events (
                severity,
                event_code,
                symbol,
                message,
                details
            )
            VALUES (
                'warning',
                'options_entry_gate_rejected',
                %s,
                %s,
                %s
            )
            """,
            (
                thesis["symbol"],
                (
                    f"Options entry blocked for {thesis['symbol']} "
                    f"{thesis['strategy']} {thesis['direction']}"
                ),
                Jsonb(
                    {
                        "trade_thesis_id": str(thesis["id"]),
                        "failures": failures,
                        "details": details,
                    }
                ),
            ),
        )

    def health_check(self):
        now = datetime.now(timezone.utc)
        clock = self.trading_client.get_clock()
        account = self.trading_client.get_account()

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                config = self.load_active_config(cursor)
                control = self.load_bot_control(cursor)
                portfolio = self.portfolio_state(cursor)
                pending = self.load_pending_theses(cursor, now, 100)

        return {
            "config_version": config["version"],
            "bot_control": dict(control),
            "alpaca": {
                "paper_client": True,
                "account_status": str(getattr(account, "status", "")),
                "options_buying_power": getattr(
                    account, "options_buying_power", None
                ),
                "options_approved_level": getattr(
                    account, "options_approved_level", None
                ),
                "options_trading_level": getattr(
                    account, "options_trading_level", None
                ),
                "market_open": bool(clock.is_open),
                "minutes_to_close": (
                    self.minutes_to_close(clock)
                    if bool(clock.is_open)
                    else None
                ),
            },
            "portfolio": dict(portfolio),
            "eligible_approved_core_active_theses": len(pending),
            "rules": {
                "dte": [MIN_DTE, MAX_DTE],
                "short_delta_abs": [
                    SHORT_DELTA_MIN,
                    SHORT_DELTA_MAX,
                ],
                "minimum_credit_to_width": MIN_CREDIT_TO_WIDTH,
                "max_loss_per_option_trade": MAX_OPTION_TRADE_LOSS,
                "max_total_option_open_risk": MAX_OPTION_PORTFOLIO_LOSS,
                "max_open_option_positions": MAX_OPEN_OPTION_POSITIONS,
                "max_combined_ideas_per_sector": MAX_COMBINED_IDEAS_PER_SECTOR,
                "no_new_entry_last_minutes": NO_NEW_ENTRY_MINUTES,
                "ai_confidence_min": MIN_AI_CONFIDENCE,
                "liquidity_proxy_v1_max_leg_bid_ask_pct_mid":
                    MAX_LEG_BID_ASK_PCT_OF_MID,
            },
            "broker_orders_submitted": False,
            "writes_performed": False,
        }

    def process(self, limit):
        now = datetime.now(timezone.utc)
        clock = self.trading_client.get_clock()

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.load_active_config(cursor)
                control = self.load_bot_control(cursor)
                theses = self.load_pending_theses(
                    cursor, now, limit
                )

            results = []

            for thesis in theses:
                with connection.cursor() as cursor:
                    failures = []
                    details = {}

                    if control["trading_mode"] != "paper":
                        failures.append("trading_mode_not_paper")
                    if control["kill_switch_active"]:
                        failures.append("kill_switch_active")
                    if not control["new_entries_enabled"]:
                        failures.append("new_entries_disabled")
                    if not bool(clock.is_open):
                        failures.append("regular_market_not_open")
                    elif self.minutes_to_close(clock) <= NO_NEW_ENTRY_MINUTES:
                        failures.append("inside_last_30_minutes")

                    ai_failures, ai_details = self.ai_gate(thesis)
                    failures.extend(ai_failures)
                    details["ai"] = ai_details

                    portfolio = self.portfolio_state(cursor)
                    open_count = int(portfolio["open_option_positions"])
                    open_risk = D(
                        portfolio["option_open_risk"], Decimal("0")
                    )
                    details["portfolio"] = {
                        "open_option_positions": open_count,
                        "option_open_risk": open_risk,
                    }

                    if open_count >= MAX_OPEN_OPTION_POSITIONS:
                        failures.append("max_open_option_positions_reached")

                    if open_risk >= MAX_OPTION_PORTFOLIO_LOSS:
                        failures.append("max_option_portfolio_risk_reached")

                    if (
                        self.current_symbol_option_positions(
                            cursor, thesis["symbol"]
                        )
                        >= MAX_OPTION_POSITIONS_PER_SYMBOL
                    ):
                        failures.append(
                            "option_position_for_symbol_already_open"
                        )

                    pending_symbol_intent = (
                        self.has_pending_option_intent_for_symbol(
                            cursor, thesis["symbol"]
                        )
                    )
                    if pending_symbol_intent is not None:
                        failures.append(
                            "active_option_entry_intent_for_symbol_exists"
                        )

                    sector_count = self.sector_idea_count(
                        cursor, thesis.get("sector")
                    )
                    details["sector"] = {
                        "sector": thesis.get("sector"),
                        "open_combined_ideas": sector_count,
                    }
                    if sector_count >= MAX_COMBINED_IDEAS_PER_SECTOR:
                        failures.append(
                            "sector_combined_idea_limit_reached"
                        )

                    earnings_failures, earnings_details = (
                        self.earnings_gate(
                            cursor,
                            thesis["symbol"],
                            now,
                        )
                    )
                    failures.extend(earnings_failures)
                    details["earnings"] = earnings_details

                    if failures:
                        unique = list(dict.fromkeys(failures))
                        self.log_block(
                            cursor,
                            thesis,
                            unique,
                            details,
                        )
                        connection.commit()

                        results.append(
                            {
                                "symbol": thesis["symbol"],
                                "strategy": thesis["strategy"],
                                "direction": thesis["direction"],
                                "status": "blocked",
                                "failures": unique,
                            }
                        )
                        continue

                    chain = self.fetch_chain(
                        thesis["symbol"],
                        now,
                    )
                    contracts = self.normalized_contracts(
                        chain,
                        now,
                    )
                    candidates = self.candidate_spreads(
                        thesis,
                        contracts,
                    )

                    details["option_chain"] = {
                        "contracts_returned": len(chain),
                        "normalized_contracts": len(contracts),
                        "eligible_spreads": len(candidates),
                    }

                    if not candidates:
                        self.log_block(
                            cursor,
                            thesis,
                            ["no_eligible_defined_risk_credit_spread"],
                            details,
                        )
                        connection.commit()

                        results.append(
                            {
                                "symbol": thesis["symbol"],
                                "strategy": thesis["strategy"],
                                "direction": thesis["direction"],
                                "status": "blocked",
                                "failures": [
                                    "no_eligible_defined_risk_credit_spread"
                                ],
                            }
                        )
                        continue

                    selected = None
                    for spread in candidates:
                        projected = open_risk + spread["max_loss"]
                        if projected <= MAX_OPTION_PORTFOLIO_LOSS:
                            selected = spread
                            break

                    if selected is None:
                        self.log_block(
                            cursor,
                            thesis,
                            ["spread_would_exceed_option_portfolio_risk"],
                            details,
                        )
                        connection.commit()
                        results.append(
                            {
                                "symbol": thesis["symbol"],
                                "status": "blocked",
                                "failures": [
                                    "spread_would_exceed_option_portfolio_risk"
                                ],
                            }
                        )
                        continue

                    intent_id = self.create_option_intent(
                        cursor,
                        thesis,
                        selected,
                        details,
                        now,
                    )

                    if intent_id is None:
                        connection.rollback()
                        results.append(
                            {
                                "symbol": thesis["symbol"],
                                "status": "skipped_duplicate_intent",
                            }
                        )
                        continue

                    connection.commit()

                    results.append(
                        {
                            "symbol": thesis["symbol"],
                            "strategy": thesis["strategy"],
                            "direction": thesis["direction"],
                            "status": "approved_option_intent_created",
                            "intent_id": str(intent_id),
                            "spread_type": (
                                "bull_put_credit_spread"
                                if thesis["direction"] == "long"
                                else "bear_call_credit_spread"
                            ),
                            "contracts": selected["contracts"],
                            "expiration_date":
                                selected["short_leg"]["expiration_date"],
                            "short_contract":
                                selected["short_leg"]["contract_symbol"],
                            "short_delta":
                                selected["short_leg"]["delta"],
                            "long_contract":
                                selected["long_leg"]["contract_symbol"],
                            "credit_per_share": selected["credit"],
                            "max_profit": selected["max_profit"],
                            "max_loss": selected["max_loss"],
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
            "DELTAX defined-risk options spread intent builder."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Read-only health check.",
    )
    mode.add_argument(
        "--process",
        action="store_true",
        help=(
            "Create eligible option-spread trade intents. "
            "Never submits broker orders."
        ),
    )
    parser.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_PROCESS_LIMIT:
        parser.error(
            f"--limit must be between 1 and {MAX_PROCESS_LIMIT}"
        )
    return args


def main():
    args = parse_args()
    builder = OptionsSpreadIntentBuilder()

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
    print("OPTIONS SPREAD INTENT BUILDER: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
