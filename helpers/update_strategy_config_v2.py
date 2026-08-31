# File: helpers/update_strategy_config_v2.py
# Purpose: Previews or applies the DELTAX v2 strategy configuration with the approved direction-routing and news-gate rules.

import argparse
import copy
import json
import os
import sys

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

SOURCE_VERSION = "deltax_v2_strategy_v1"
TARGET_VERSION = "deltax_v2_strategy_v2"
TARGET_NAME = (
    "DELTAX v2 Stock and Defined-Risk Options Strategy v2"
)
CONFIDENCE_THRESHOLD = 0.65


def load_active_config(cursor, lock_row=False):
    query = """
        SELECT
            id,
            version,
            name,
            config
        FROM strategy_configs
        WHERE is_active = true
        ORDER BY activated_at DESC NULLS LAST,
                 created_at DESC
        LIMIT 1
    """

    if lock_row:
        query += " FOR UPDATE"

    cursor.execute(query)
    return cursor.fetchone()


def load_target_config(cursor):
    cursor.execute(
        """
        SELECT
            id,
            version,
            name,
            config,
            is_active
        FROM strategy_configs
        WHERE version = %s
        LIMIT 1
        """,
        (TARGET_VERSION,),
    )

    return cursor.fetchone()


def build_updated_config(source_config):
    config = copy.deepcopy(source_config)
    config["schema_version"] = 2

    ai_gate = config.setdefault("ai_gate", {})
    ai_gate["required"] = True
    ai_gate["may_reject_trade"] = True
    ai_gate["may_change_deterministic_risk_limits"] = False
    ai_gate["allowed_directions"] = [
        "bullish",
        "bearish",
        "neutral",
    ]
    ai_gate["directional_confidence_threshold"] = (
        CONFIDENCE_THRESHOLD
    )
    ai_gate["required_outputs"] = [
        "direction",
        "confidence",
        "meaningful_company_specific_catalyst",
        "sufficient_news",
        "time_horizon",
        "catalyst",
        "risks",
        "invalidation_condition",
    ]

    scanner = config.setdefault("scanner", {})
    scanner.pop("price_confirmation_minutes", None)
    scanner["interval_minutes"] = 5
    scanner["confirmation_minutes_by_strategy"] = {
        "core": 10,
        "active": 10,
        "intraday": 0,
    }
    scanner["new_entries_cutoff_minutes_before_close"] = 30

    config["news_rules"] = {
        "directional_confidence_threshold": CONFIDENCE_THRESHOLD,
        "material_news_requires": {
            "meaningful_company_specific_catalyst": True,
            "sufficient_news": True,
            "direction_in": ["bullish", "bearish"],
        },
        "fail_closed_on_unprocessed_fresh_news": True,
        "fail_closed_on_conflicting_material_news": True,
        "regular_session_anchor": (
            "first_tradable_minute_after_published_at"
        ),
        "outside_session_anchor": "next_regular_session_open",
        "technical_signal_anchor": "technical_signal_time",
        "event_clustering": {
            "premarket_same_session_open": True,
            "regular_session_window_minutes": 15,
        },
        "intraday_active_window": {
            "regular_session_lookback_minutes": 60,
            "premarket_starts_at_previous_session_close": True,
            "material_premarket_news_active_for_full_session": True,
        },
    }

    config["direction_router"] = {
        "core_active": {
            "confirmation_required": True,
            "confirmation_minutes": 10,
            "modes": ["mean_reversion", "news_momentum"],
            "downside_deviation": {
                "no_material_bearish_and_price_up": {
                    "direction": "long",
                    "mode": "mean_reversion",
                },
                "material_bearish_and_price_down": {
                    "direction": "short",
                    "mode": "news_momentum",
                },
            },
            "upside_deviation": {
                "no_material_bullish_and_price_down": {
                    "direction": "short",
                    "mode": "mean_reversion",
                },
                "material_bullish_and_price_up": {
                    "direction": "long",
                    "mode": "news_momentum",
                },
            },
            "reject_on_news_price_conflict": True,
            "material_bearish_blocks_long_mean_reversion": True,
            "material_bullish_blocks_short_mean_reversion": True,
        },
        "intraday": {
            "mode": "intraday_mean_reversion",
            "confirmation_required": False,
            "execute_in_same_scan_cycle": True,
            "downside_technical_direction": "long",
            "upside_technical_direction": "short",
            "bearish_news_vetoes_long": True,
            "bullish_news_vetoes_short": True,
            "news_may_reverse_technical_direction": False,
            "neutral_generic_or_insufficient_news_vetoes": False,
        },
        "price_confirmation": {
            "long_passes_when": "confirmation_price > signal_price",
            "short_passes_when": "confirmation_price < signal_price",
            "equal_price_passes": False,
        },
    }

    short_rules = config.setdefault("short_rules", {})
    short_rules.pop("confirmation_minutes", None)
    short_rules.pop("requires_bearish_ai_catalyst", None)
    short_rules.pop("requires_price_reversal_confirmation", None)
    short_rules.pop("requires_atr_confirmation", None)
    short_rules["enabled"] = True
    short_rules["requires_alpaca_shortable"] = True
    short_rules["requires_alpaca_easy_to_borrow"] = True
    short_rules["thresholds_may_only_be_tightened"] = True
    short_rules["requires_market_and_sector_alignment"] = True
    short_rules["core_active"] = {
        "confirmation_required": True,
        "confirmation_minutes": 10,
        "mean_reversion_without_bearish_catalyst_allowed": True,
        "bearish_news_momentum_allowed": True,
    }
    short_rules["intraday"] = {
        "confirmation_required": False,
        "requires_separate_upside_technical_signal": True,
        "active_bullish_news_veto": True,
    }

    options = config.setdefault("options", {})
    options["allowed_stock_strategies"] = ["core", "active"]
    options["intraday_allowed"] = False
    options["requires_fresh_material_company_catalyst"] = True
    options["requires_sufficient_news"] = True
    options["directional_confidence_threshold"] = (
        CONFIDENCE_THRESHOLD
    )
    options["allowed_ai_directions"] = ["bullish", "bearish"]
    options["neutral_direction_allowed"] = False
    options["price_confirmation_required"] = True
    options["price_confirmation_minutes"] = 10
    options["bullish_structure"] = "bull_put_credit_spread"
    options["bearish_structure"] = "bear_call_credit_spread"

    stock_strategies = config.setdefault("stock_strategies", {})

    for strategy_name in ("core", "active"):
        strategy = stock_strategies.setdefault(strategy_name, {})
        strategy["direction_selected_by_router"] = True
        strategy["confirmation_required"] = True
        strategy["confirmation_minutes"] = 10
        strategy["allowed_modes"] = [
            "mean_reversion",
            "news_momentum",
        ]

    intraday = stock_strategies.setdefault("intraday", {})
    intraday["direction_selected_by_technical_signal"] = True
    intraday["confirmation_required"] = False
    intraday["news_used_as_adverse_direction_veto_only"] = True
    intraday["active_regular_news_lookback_minutes"] = 60
    intraday["material_premarket_news_active_full_session"] = True

    return config


def validate_updated_config(config):
    errors = []

    if config.get("schema_version") != 2:
        errors.append("schema_version_must_be_2")

    scanner = config.get("scanner", {})
    confirmation = scanner.get(
        "confirmation_minutes_by_strategy",
        {},
    )

    if confirmation != {
        "core": 10,
        "active": 10,
        "intraday": 0,
    }:
        errors.append("invalid_strategy_confirmation_minutes")

    router = config.get("direction_router", {})

    if not router.get("core_active"):
        errors.append("missing_core_active_router")

    if not router.get("intraday"):
        errors.append("missing_intraday_router")

    news_rules = config.get("news_rules", {})

    if (
        news_rules.get("directional_confidence_threshold")
        != CONFIDENCE_THRESHOLD
    ):
        errors.append("invalid_news_confidence_threshold")

    options = config.get("options", {})

    if options.get("bearish_structure") != "bear_call_credit_spread":
        errors.append("missing_bearish_options_branch")

    if options.get("intraday_allowed") is not False:
        errors.append("intraday_options_must_be_disabled")

    if errors:
        raise RuntimeError(
            "Configuration validation failed: "
            + ", ".join(errors)
        )


def print_preview(source_version, config):
    print("DELTAX STRATEGY CONFIG V2 PREVIEW")
    print(f"Source version: {source_version}")
    print(f"Target version: {TARGET_VERSION}")
    print("Database changes: no")
    print("\nPLANNED LOGIC")
    print("- Core confirmation: 10 minutes")
    print("- Active confirmation: 10 minutes")
    print("- Intraday confirmation: 0 minutes")
    print("- Core/Active modes: mean_reversion, news_momentum")
    print("- Intraday mode: intraday_mean_reversion")
    print("- Material-news confidence threshold: 0.65")
    print("- Intraday regular-session news window: 60 minutes")
    print("- Material premarket news remains active all session")
    print("- Conflicting or unprocessed fresh news fails closed")
    print("- Bullish options branch: bull put credit spread")
    print("- Bearish options branch: bear call credit spread")
    print("- Intraday options remain disabled")
    print("\nTOP-LEVEL CONFIG KEYS")

    for key in sorted(config):
        print(f"- {key}")


def apply_config(connection, source_row, target_config):
    source_id, source_version, _, _ = source_row

    if source_version not in {SOURCE_VERSION, TARGET_VERSION}:
        raise RuntimeError(
            "Unexpected active strategy version: "
            f"{source_version}"
        )

    with connection.cursor() as cursor:
        existing_target = load_target_config(cursor)

        if existing_target is not None:
            existing_config = existing_target[3]
            existing_is_active = existing_target[4]

            if existing_config != target_config:
                raise RuntimeError(
                    f"{TARGET_VERSION} already exists with different config"
                )

            if existing_is_active:
                print(f"{TARGET_VERSION} is already active.")
                return False

            cursor.execute(
                """
                UPDATE strategy_configs
                SET
                    is_active = false,
                    activated_at = NULL
                WHERE is_active = true
                """
            )
            cursor.execute(
                """
                UPDATE strategy_configs
                SET
                    is_active = true,
                    activated_at = now()
                WHERE version = %s
                """,
                (TARGET_VERSION,),
            )
        else:
            cursor.execute(
                """
                UPDATE strategy_configs
                SET
                    is_active = false,
                    activated_at = NULL
                WHERE is_active = true
                """
            )
            cursor.execute(
                """
                INSERT INTO strategy_configs (
                    version,
                    name,
                    config,
                    is_active,
                    activated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    true,
                    now()
                )
                """,
                (
                    TARGET_VERSION,
                    TARGET_NAME,
                    Jsonb(target_config),
                ),
            )

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM strategy_configs
            WHERE is_active = true
            """
        )
        active_count = cursor.fetchone()[0]

        if active_count != 1:
            raise RuntimeError(
                f"Expected one active config, found {active_count}"
            )

        cursor.execute(
            """
            SELECT version
            FROM strategy_configs
            WHERE is_active = true
            """
        )
        active_version = cursor.fetchone()[0]

        if active_version != TARGET_VERSION:
            raise RuntimeError(
                "Target configuration was not activated"
            )

    print(f"Previous active config id: {source_id}")
    return True


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Preview or apply the DELTAX strategy configuration v2."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write and activate the v2 configuration.",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the complete proposed configuration JSON.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            source_row = load_active_config(
                cursor,
                lock_row=args.apply,
            )

        if source_row is None:
            raise RuntimeError("No active strategy configuration found")

        source_version = source_row[1]
        source_config = source_row[3]

        if source_version not in {SOURCE_VERSION, TARGET_VERSION}:
            raise RuntimeError(
                "Expected active version "
                f"{SOURCE_VERSION} or {TARGET_VERSION}, "
                f"found {source_version}"
            )

        target_config = build_updated_config(source_config)
        validate_updated_config(target_config)
        print_preview(source_version, target_config)

        if args.print_json:
            print("\nPROPOSED CONFIG JSON")
            print(
                json.dumps(
                    target_config,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
            )

        if not args.apply:
            connection.rollback()
            print("\nPREVIEW COMPLETE: NO DATABASE CHANGES")
            print(
                "Run with --apply only after reviewing the preview."
            )
            return

        changed = apply_config(
            connection,
            source_row,
            target_config,
        )
        connection.commit()

        if changed:
            print(f"\nACTIVE CONFIG: {TARGET_VERSION}")
            print("Previous configuration retained as inactive.")
        else:
            print("\nNo configuration change was required.")

        print("STRATEGY CONFIG V2 UPDATE: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        sys.exit(1)
