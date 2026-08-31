# File: helpers/update_market_news_schema.py
# Purpose: Previews or applies the schema extension required for symbol-free market event clusters.

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv


TARGET_COLUMNS = {
    "scope": {
        "data_type": "text",
        "nullable": "NO",
        "default_contains": "symbol",
    },
    "analysis_status": {
        "data_type": "text",
        "nullable": "NO",
        "default_contains": "pending",
    },
    "analysis_metadata": {
        "data_type": "jsonb",
        "nullable": "NO",
        "default_contains": "{}",
    },
}

TARGET_CONSTRAINTS = {
    "event_clusters_scope_check",
    "event_clusters_scope_primary_symbol_check",
    "event_clusters_analysis_status_check",
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or apply DELTAX market-news schema support."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration. Without this flag, only a read-only preview is shown.",
    )
    return parser.parse_args()


def get_columns(connection: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                column_name,
                data_type,
                is_nullable,
                column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'event_clusters'
            ORDER BY ordinal_position
            """
        )
        return {
            row[0]: {
                "data_type": row[1],
                "nullable": row[2],
                "default": row[3],
            }
            for row in cursor.fetchall()
        }


def get_constraints(connection: psycopg.Connection[Any]) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                constraint_name,
                pg_get_constraintdef(pg_constraint.oid)
            FROM information_schema.table_constraints
            JOIN pg_constraint
              ON pg_constraint.conname = constraint_name
            JOIN pg_namespace
              ON pg_namespace.oid = pg_constraint.connamespace
             AND pg_namespace.nspname = constraint_schema
            WHERE table_schema = 'public'
              AND table_name = 'event_clusters'
            ORDER BY constraint_name
            """
        )
        return {row[0]: row[1] for row in cursor.fetchall()}


def get_counts(
    connection: psycopg.Connection[Any],
    columns: dict[str, dict[str, Any]],
) -> dict[str, int]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM event_clusters")
        total = cursor.fetchone()[0]

        cursor.execute(
            "SELECT count(*) FROM event_clusters WHERE primary_symbol IS NOT NULL"
        )
        with_symbol = cursor.fetchone()[0]

        cursor.execute(
            "SELECT count(*) FROM event_clusters WHERE primary_symbol IS NULL"
        )
        without_symbol = cursor.fetchone()[0]

        counts = {
            "total_event_clusters": total,
            "clusters_with_primary_symbol": with_symbol,
            "clusters_without_primary_symbol": without_symbol,
        }

        if "scope" in columns:
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (WHERE scope = 'symbol'),
                    count(*) FILTER (WHERE scope = 'market')
                FROM event_clusters
                """
            )
            symbol_scope, market_scope = cursor.fetchone()
            counts["symbol_scope_clusters"] = symbol_scope
            counts["market_scope_clusters"] = market_scope

        return counts


def validate_base_schema(columns: dict[str, dict[str, Any]]) -> None:
    required = {
        "id",
        "cluster_key",
        "primary_symbol",
        "event_type",
        "status",
        "first_published_at",
        "last_published_at",
        "created_at",
        "updated_at",
    }
    missing = sorted(required - set(columns))
    if missing:
        raise RuntimeError(
            "event_clusters is missing required base columns: " + ", ".join(missing)
        )


def migration_needed(
    columns: dict[str, dict[str, Any]],
    constraints: dict[str, str],
) -> bool:
    primary_symbol = columns.get("primary_symbol", {})
    if primary_symbol.get("nullable") != "YES":
        return True

    for name, expected in TARGET_COLUMNS.items():
        actual = columns.get(name)
        if actual is None:
            return True
        if actual.get("data_type") != expected["data_type"]:
            return True
        if actual.get("nullable") != expected["nullable"]:
            return True
        if expected["default_contains"] not in str(actual.get("default") or ""):
            return True

    return not TARGET_CONSTRAINTS.issubset(constraints)


def print_preview(
    columns: dict[str, dict[str, Any]],
    constraints: dict[str, str],
    counts: dict[str, int],
    apply_requested: bool,
) -> None:
    primary_nullable = columns["primary_symbol"]["nullable"] == "YES"
    missing_columns = sorted(set(TARGET_COLUMNS) - set(columns))
    missing_constraints = sorted(TARGET_CONSTRAINTS - set(constraints))

    print("DELTAX MARKET NEWS SCHEMA PREVIEW")
    print(f"Mode: {'APPLY' if apply_requested else 'PREVIEW'}")
    print(json.dumps(counts, indent=2))
    print("\nCURRENT COMPATIBILITY")
    print(f"- primary_symbol accepts NULL: {'yes' if primary_nullable else 'no'}")
    print(
        "- missing columns: "
        + (", ".join(missing_columns) if missing_columns else "none")
    )
    print(
        "- missing constraints: "
        + (", ".join(missing_constraints) if missing_constraints else "none")
    )
    print("\nPLANNED CHANGES")
    print("- Preserve all existing event clusters and symbol relationships")
    print("- Allow primary_symbol to be NULL for market-wide events")
    print("- Add scope: symbol or market")
    print("- Add analysis_status: pending, processing, completed, failed, or skipped")
    print("- Add analysis_metadata JSONB for one market-event AI result")
    print("- Enforce that symbol scope has a symbol and market scope has no symbol")
    print("- Add indexes for pending market-event processing")
    print("\nNo source events, AI analyses, trade theses, intents, or orders are changed.")


def apply_migration(connection: psycopg.Connection[Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            ALTER TABLE event_clusters
                ADD COLUMN IF NOT EXISTS scope text,
                ADD COLUMN IF NOT EXISTS analysis_status text,
                ADD COLUMN IF NOT EXISTS analysis_metadata jsonb;

            ALTER TABLE event_clusters
                ALTER COLUMN primary_symbol DROP NOT NULL;

            UPDATE event_clusters
            SET scope = CASE
                WHEN primary_symbol IS NULL THEN 'market'
                ELSE 'symbol'
            END
            WHERE scope IS NULL;

            UPDATE event_clusters
            SET analysis_status = 'pending'
            WHERE analysis_status IS NULL;

            UPDATE event_clusters
            SET analysis_metadata = '{}'::jsonb
            WHERE analysis_metadata IS NULL;

            ALTER TABLE event_clusters
                ALTER COLUMN scope SET DEFAULT 'symbol',
                ALTER COLUMN scope SET NOT NULL,
                ALTER COLUMN analysis_status SET DEFAULT 'pending',
                ALTER COLUMN analysis_status SET NOT NULL,
                ALTER COLUMN analysis_metadata SET DEFAULT '{}'::jsonb,
                ALTER COLUMN analysis_metadata SET NOT NULL;
            """
        )

        cursor.execute(
            """
            DO $migration$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'event_clusters_scope_check'
                      AND conrelid = 'event_clusters'::regclass
                ) THEN
                    ALTER TABLE event_clusters
                    ADD CONSTRAINT event_clusters_scope_check
                    CHECK (scope IN ('symbol', 'market'));
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'event_clusters_scope_primary_symbol_check'
                      AND conrelid = 'event_clusters'::regclass
                ) THEN
                    ALTER TABLE event_clusters
                    ADD CONSTRAINT event_clusters_scope_primary_symbol_check
                    CHECK (
                        (scope = 'symbol' AND primary_symbol IS NOT NULL)
                        OR
                        (scope = 'market' AND primary_symbol IS NULL)
                    );
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'event_clusters_analysis_status_check'
                      AND conrelid = 'event_clusters'::regclass
                ) THEN
                    ALTER TABLE event_clusters
                    ADD CONSTRAINT event_clusters_analysis_status_check
                    CHECK (
                        analysis_status IN (
                            'pending',
                            'processing',
                            'completed',
                            'failed',
                            'skipped'
                        )
                    );
                END IF;
            END
            $migration$;

            CREATE INDEX IF NOT EXISTS idx_event_clusters_market_pending
                ON event_clusters (analysis_status, last_published_at DESC)
                WHERE scope = 'market';

            CREATE INDEX IF NOT EXISTS idx_event_clusters_scope_status
                ON event_clusters (scope, status, last_published_at DESC);
            """
        )


def validate_result(
    columns: dict[str, dict[str, Any]],
    constraints: dict[str, str],
) -> None:
    if migration_needed(columns, constraints):
        raise RuntimeError("Post-migration validation failed; transaction will be rolled back.")


def main() -> int:
    args = parse_args()
    load_dotenv(project_root() / ".env")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: DATABASE_URL is missing from .env.", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(database_url) as connection:
            columns = get_columns(connection)
            validate_base_schema(columns)
            constraints = get_constraints(connection)
            counts = get_counts(connection, columns)
            needed = migration_needed(columns, constraints)
            print_preview(columns, constraints, counts, args.apply)

            if not args.apply:
                connection.rollback()
                print(f"\nMigration required: {'yes' if needed else 'no'}")
                print("PREVIEW COMPLETE: NO DATABASE CHANGES")
                return 0

            if not needed:
                connection.rollback()
                print("\nSchema is already compatible; no changes were required.")
                print("MARKET NEWS SCHEMA UPDATE: OK")
                return 0

            apply_migration(connection)
            updated_columns = get_columns(connection)
            updated_constraints = get_constraints(connection)
            validate_result(updated_columns, updated_constraints)
            connection.commit()

            print("\nApplied successfully:")
            print("- Existing clusters retained")
            print("- Market-scope clusters supported")
            print("- Pending market-event processing can be tracked")
            print("MARKET NEWS SCHEMA UPDATE: OK")
            return 0
    except (RuntimeError, psycopg.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
