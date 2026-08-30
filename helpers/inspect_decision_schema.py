# File: helpers/inspect_decision_schema.py
# Purpose: Prints the Neon tables, columns, and constraints required for scanner decisions, risk checks, trade intents, orders, positions, and audit records.

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

NAME_PATTERNS = [
    "scan",
    "signal",
    "decision",
    "intent",
    "order",
    "position",
    "ledger",
    "risk",
    "market",
    "news",
    "analysis",
    "event",
]


def is_relevant(table_name: str) -> bool:
    lowered = table_name.lower()

    return any(
        pattern in lowered
        for pattern in NAME_PATTERNS
    )


if __name__ == "__main__":
    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=10,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                ORDER BY table_name;
                """
            )

            all_tables = [
                row[0]
                for row in cursor.fetchall()
            ]

            relevant_tables = [
                table
                for table in all_tables
                if is_relevant(table)
            ]

            print("ALL PUBLIC TABLES")
            print(", ".join(all_tables))

            print("\nRELEVANT TABLES")
            print(", ".join(relevant_tables))

            for table_name in relevant_tables:
                print(f"\n{'=' * 70}")
                print(f"TABLE: {table_name}")
                print("=" * 70)

                cursor.execute(
                    """
                    SELECT
                        ordinal_position,
                        column_name,
                        data_type,
                        is_nullable,
                        column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = %s
                    ORDER BY ordinal_position;
                    """,
                    (table_name,),
                )

                print("\nCOLUMNS")

                for row in cursor.fetchall():
                    (
                        position,
                        column,
                        data_type,
                        nullable,
                        default,
                    ) = row

                    print(
                        f"{position}. {column} "
                        f"| {data_type} "
                        f"| nullable={nullable} "
                        f"| default={default}"
                    )

                cursor.execute(
                    """
                    SELECT
                        pg_constraint.conname AS constraint_name,
                        pg_get_constraintdef(
                            pg_constraint.oid
                        )
                    FROM pg_constraint
                    JOIN pg_class
                        ON pg_class.oid =
                           pg_constraint.conrelid
                    JOIN pg_namespace
                        ON pg_namespace.oid =
                           pg_class.relnamespace
                    WHERE pg_namespace.nspname = 'public'
                      AND pg_class.relname = %s
                    ORDER BY constraint_name;
                    """,
                    (table_name,),
                )

                print("\nCONSTRAINTS")

                constraints = cursor.fetchall()

                if not constraints:
                    print("None")
                else:
                    for name, definition in constraints:
                        print(f"{name}: {definition}")

    print("\nSCHEMA INSPECTION: OK")