# File: helpers/setup_sp500_universe.py
# Purpose: Create/update a Neon universe named "sp500_scan" from the current
# S&P 500 constituents, validating every symbol against Alpaca before writing.
#
# Modes:
#   --check    : DB/schema + current universe status only. No web/Alpaca fetch.
#   --dry-run  : Fetch current S&P 500 list + Alpaca assets and show diff. No DB writes.
#   --apply    : Upsert instruments, universe, and memberships. Does NOT change
#                strategy config or scanner behavior yet.
#
# External constituent source:
#   Wikipedia "List of S&P 500 companies" table. Official S&P DJI reports
#   503 constituents as of Jul 31, 2026; this script treats ~503 as expected
#   and prints a warning if the fetched source differs materially.

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg
import requests
from html.parser import HTMLParser
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]

UNIVERSE_CODE = "sp500_scan"
UNIVERSE_NAME = "S&P 500 Technical Scan Universe"
SOURCE_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
EXPECTED_CONSTITUENTS = 503
REQUEST_TIMEOUT_SECONDS = 30


def json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class _WikipediaSP500Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_target_table = False
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.cell_parts = []
        self.current_row = []
        self.rows = []
        self.header_seen = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag == "table":
            if not self.in_target_table and attrs.get("id") == "constituents":
                self.in_target_table = True
                self.table_depth = 1
                return

            if self.in_target_table:
                self.table_depth += 1

        if not self.in_target_table:
            return

        if tag == "tr":
            self.in_row = True
            self.current_row = []
        elif tag in {"td", "th"} and self.in_row:
            self.in_cell = True
            self.cell_parts = []

    def handle_endtag(self, tag):
        if not self.in_target_table:
            return

        if tag in {"td", "th"} and self.in_cell:
            text = " ".join("".join(self.cell_parts).split())
            self.current_row.append(text)
            self.in_cell = False
            self.cell_parts = []

        elif tag == "tr" and self.in_row:
            if self.current_row:
                if not self.header_seen:
                    normalized = {cell.strip() for cell in self.current_row}
                    if {"Symbol", "Security", "GICS Sector"}.issubset(normalized):
                        self.header_seen = True
                else:
                    self.rows.append(self.current_row)
            self.in_row = False
            self.current_row = []

        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_target_table = False

    def handle_data(self, data):
        if self.in_target_table and self.in_cell:
            self.cell_parts.append(data)


def fetch_sp500():
    response = requests.get(
        SOURCE_URL,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={
            "User-Agent": "DELTAX-v2/1.0 (+S&P500 technical universe refresh)"
        },
    )
    response.raise_for_status()

    parser = _WikipediaSP500Parser()
    parser.feed(response.text)

    if not parser.header_seen or not parser.rows:
        raise RuntimeError(
            "Could not identify the S&P 500 constituents table "
            "in the fetched HTML"
        )

    rows = []
    seen = set()

    # Current Wikipedia table columns:
    # Symbol, Security, GICS Sector, GICS Sub-Industry, Headquarters,
    # Date added, CIK, Founded
    for row in parser.rows:
        if len(row) < 4:
            continue

        symbol = row[0].strip().upper().replace("\\xa0", "")
        company = row[1].strip()
        sector = row[2].strip()
        industry = row[3].strip()

        if not symbol or symbol in seen:
            continue

        seen.add(symbol)
        rows.append(
            {
                "symbol": symbol,
                "company_name": company,
                "sector": sector or None,
                "industry": industry or None,
            }
        )

    return rows


def alpaca_assets():
    client = TradingClient(
        ALPACA_API_KEY,
        ALPACA_API_SECRET,
        paper=True,
    )

    assets = client.get_all_assets(
        GetAssetsRequest(
            status=AssetStatus.ACTIVE,
            asset_class=AssetClass.US_EQUITY,
        )
    )

    result = {}
    for asset in assets:
        symbol = str(asset.symbol).upper()
        result[symbol] = {
            "symbol": symbol,
            "name": getattr(asset, "name", None),
            "tradable": bool(getattr(asset, "tradable", False)),
            "shortable": bool(getattr(asset, "shortable", False)),
            "easy_to_borrow": bool(getattr(asset, "easy_to_borrow", False)),
            "fractionable": bool(getattr(asset, "fractionable", False)),
            "marginable": bool(getattr(asset, "marginable", False)),
        }

    return result


def db_status(cursor):
    cursor.execute(
        """
        SELECT id, code, name, is_active, metadata
        FROM universes
        WHERE code IN ('alyrise_base', %s)
        ORDER BY code
        """,
        (UNIVERSE_CODE,),
    )
    universes = cursor.fetchall()

    cursor.execute(
        """
        SELECT
            universes.code,
            COUNT(*) FILTER (
                WHERE memberships.is_enabled = true
                  AND (
                      memberships.eligible_until IS NULL
                      OR memberships.eligible_until > now()
                  )
            ) AS enabled_members
        FROM universes
        LEFT JOIN universe_memberships memberships
          ON memberships.universe_id = universes.id
        WHERE universes.code IN ('alyrise_base', %s)
        GROUP BY universes.code
        ORDER BY universes.code
        """,
        (UNIVERSE_CODE,),
    )
    counts = cursor.fetchall()

    return {
        "universes": [dict(row) for row in universes],
        "membership_counts": {
            row["code"]: int(row["enabled_members"])
            for row in counts
        },
    }


def health_check():
    with psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name IN (
                      'instruments',
                      'universes',
                      'universe_memberships'
                  )
                """
            )
            actual = {}
            for row in cursor.fetchall():
                actual.setdefault(row["table_name"], set()).add(row["column_name"])

            required = {
                "instruments": {
                    "symbol",
                    "alpaca_symbol",
                    "company_name",
                    "asset_type",
                    "sector",
                    "industry",
                    "is_trade_candidate",
                    "is_market_proxy",
                    "stock_enabled",
                    "options_enabled",
                    "alpaca_tradable",
                    "alpaca_shortable",
                    "alpaca_easy_to_borrow",
                    "alpaca_fractionable",
                    "alpaca_marginable",
                    "last_validated_at",
                    "metadata",
                },
                "universes": {
                    "id",
                    "code",
                    "name",
                    "description",
                    "asset_class",
                    "universe_type",
                    "is_dynamic",
                    "is_active",
                    "metadata",
                },
                "universe_memberships": {
                    "universe_id",
                    "symbol",
                    "is_enabled",
                    "rank",
                    "source",
                    "eligible_from",
                    "eligible_until",
                    "metadata",
                },
            }

            missing = {
                table: sorted(columns - actual.get(table, set()))
                for table, columns in required.items()
                if columns - actual.get(table, set())
            }

            if missing:
                raise RuntimeError(
                    f"Database schema missing required columns: {missing}"
                )

            status = db_status(cursor)

    return {
        "status": "ok",
        "target_universe": UNIVERSE_CODE,
        "database": status,
        "remote_requests_performed": 0,
        "database_writes_performed": False,
        "strategy_config_changed": False,
        "scanner_changed": False,
    }


def build_plan():
    constituents = fetch_sp500()
    assets = alpaca_assets()

    validated = []
    missing_from_alpaca = []
    not_tradable = []

    for index, item in enumerate(constituents, start=1):
        asset = assets.get(item["symbol"])

        if asset is None:
            missing_from_alpaca.append(item["symbol"])
            continue

        if not asset["tradable"]:
            not_tradable.append(item["symbol"])
            continue

        validated.append(
            {
                **item,
                "rank": index,
                "alpaca": asset,
            }
        )

    return {
        "constituents": constituents,
        "validated": validated,
        "missing_from_alpaca": missing_from_alpaca,
        "not_tradable": not_tradable,
    }


def apply_plan(plan):
    validated = plan["validated"]
    current_symbols = {item["symbol"] for item in validated}
    now = datetime.now(timezone.utc)

    with psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            for item in validated:
                asset = item["alpaca"]

                cursor.execute(
                    """
                    INSERT INTO instruments (
                        symbol,
                        alpaca_symbol,
                        company_name,
                        asset_type,
                        sector,
                        industry,
                        is_trade_candidate,
                        is_market_proxy,
                        stock_enabled,
                        options_enabled,
                        alpaca_tradable,
                        alpaca_shortable,
                        alpaca_easy_to_borrow,
                        alpaca_fractionable,
                        alpaca_marginable,
                        last_validated_at,
                        metadata
                    )
                    VALUES (
                        %s, %s, %s, 'stock',
                        %s, %s,
                        true, false, true, false,
                        %s, %s, %s, %s, %s,
                        %s,
                        %s
                    )
                    ON CONFLICT (symbol)
                    DO UPDATE SET
                        alpaca_symbol = EXCLUDED.alpaca_symbol,
                        company_name = COALESCE(
                            EXCLUDED.company_name,
                            instruments.company_name
                        ),
                        sector = COALESCE(
                            EXCLUDED.sector,
                            instruments.sector
                        ),
                        industry = COALESCE(
                            EXCLUDED.industry,
                            instruments.industry
                        ),
                        is_trade_candidate = true,
                        stock_enabled = true,
                        alpaca_tradable = EXCLUDED.alpaca_tradable,
                        alpaca_shortable = EXCLUDED.alpaca_shortable,
                        alpaca_easy_to_borrow = EXCLUDED.alpaca_easy_to_borrow,
                        alpaca_fractionable = EXCLUDED.alpaca_fractionable,
                        alpaca_marginable = EXCLUDED.alpaca_marginable,
                        last_validated_at = EXCLUDED.last_validated_at,
                        metadata = instruments.metadata || EXCLUDED.metadata
                    """,
                    (
                        item["symbol"],
                        item["symbol"],
                        item["company_name"],
                        item["sector"],
                        item["industry"],
                        asset["tradable"],
                        asset["shortable"],
                        asset["easy_to_borrow"],
                        asset["fractionable"],
                        asset["marginable"],
                        now,
                        Jsonb(
                            {
                                "sp500_member": True,
                                "sp500_source": SOURCE_URL,
                                "sp500_rank_source_order": item["rank"],
                                "universe_owner": UNIVERSE_CODE,
                            }
                        ),
                    ),
                )

            cursor.execute(
                """
                INSERT INTO universes (
                    code,
                    name,
                    description,
                    asset_class,
                    universe_type,
                    is_dynamic,
                    is_active,
                    metadata
                )
                VALUES (
                    %s,
                    %s,
                    'Current S&P 500 constituents used only as the broad technical pre-filter universe.',
                    'stock',
                    'base',
                    true,
                    true,
                    %s
                )
                ON CONFLICT (code)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    asset_class = EXCLUDED.asset_class,
                    universe_type = EXCLUDED.universe_type,
                    is_dynamic = EXCLUDED.is_dynamic,
                    is_active = EXCLUDED.is_active,
                    metadata = universes.metadata || EXCLUDED.metadata
                RETURNING id
                """,
                (
                    UNIVERSE_CODE,
                    UNIVERSE_NAME,
                    Jsonb(
                        {
                            "purpose": "technical_scan_only",
                            "expected_constituents": EXPECTED_CONSTITUENTS,
                            "source_url": SOURCE_URL,
                            "last_refresh_at": now.isoformat(),
                        }
                    ),
                ),
            )
            universe_id = cursor.fetchone()["id"]

            for item in validated:
                cursor.execute(
                    """
                    INSERT INTO universe_memberships (
                        universe_id,
                        symbol,
                        is_enabled,
                        rank,
                        source,
                        eligible_from,
                        eligible_until,
                        metadata
                    )
                    VALUES (
                        %s, %s, true, %s,
                        'setup_sp500_universe',
                        now(),
                        NULL,
                        %s
                    )
                    ON CONFLICT (universe_id, symbol)
                    DO UPDATE SET
                        is_enabled = true,
                        rank = EXCLUDED.rank,
                        source = EXCLUDED.source,
                        eligible_until = NULL,
                        metadata = universe_memberships.metadata || EXCLUDED.metadata
                    """,
                    (
                        universe_id,
                        item["symbol"],
                        item["rank"],
                        Jsonb(
                            {
                                "sp500_member": True,
                                "source_url": SOURCE_URL,
                            }
                        ),
                    ),
                )

            # Disable stale former members rather than deleting audit history.
            cursor.execute(
                """
                UPDATE universe_memberships
                SET
                    is_enabled = false,
                    eligible_until = now(),
                    source = 'setup_sp500_universe_removed',
                    metadata = metadata || %s
                WHERE universe_id = %s
                  AND symbol <> ALL(%s)
                  AND is_enabled = true
                """,
                (
                    Jsonb(
                        {
                            "sp500_member": False,
                            "removed_from_source_at": now.isoformat(),
                        }
                    ),
                    universe_id,
                    list(current_symbols),
                ),
            )
            disabled_members = cursor.rowcount

        connection.commit()

        with connection.cursor() as cursor:
            status = db_status(cursor)

    return {
        "disabled_stale_members": disabled_members,
        "database": status,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create/update DELTAX S&P 500 technical-scan universe."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.check:
        result = health_check()
        print(json.dumps(result, indent=2, default=json_default))
        print("S&P 500 UNIVERSE CHECK: OK")
        return

    plan = build_plan()

    source_count = len(plan["constituents"])
    validated_count = len(plan["validated"])

    result = {
        "mode": "apply" if args.apply else "dry_run",
        "source": SOURCE_URL,
        "official_reference_constituents": EXPECTED_CONSTITUENTS,
        "source_constituents": source_count,
        "alpaca_tradable_constituents": validated_count,
        "missing_from_alpaca": plan["missing_from_alpaca"],
        "not_tradable": plan["not_tradable"],
        "count_warning": (
            abs(source_count - EXPECTED_CONSTITUENTS) > 5
        ),
        "database_writes_performed": False,
        "strategy_config_changed": False,
        "scanner_changed": False,
    }

    if args.apply:
        result["apply"] = apply_plan(plan)
        result["database_writes_performed"] = True

    print(json.dumps(result, indent=2, default=json_default))

    if validated_count < 490:
        print(
            "ERROR: Too few Alpaca-tradable S&P 500 symbols. "
            "Do not switch the scanner universe.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("S&P 500 UNIVERSE SETUP: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
