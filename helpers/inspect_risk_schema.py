# File: helpers/inspect_risk_schema.py
# Purpose: Prints the remaining DELTAX tables required to build
# production trade-intent and deterministic risk-gate logic safely.

import os

import psycopg
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

TABLES = [
    "bot_control",
    "cooldowns",
    "risk_events",
    "trade_intent_legs",
    "option_quote_snapshots",
    "broker_order_legs",
    "broker_order_events",
    "earnings_events",
    "position_legs",
    "position_snapshots",
]


def print_columns(cursor, table_name):
    cursor.execute(
        """
        SELECT
            ordinal_position,
            column_name,
            data_type,
            udt_name,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )

    print("\nCOLUMNS")
    rows = cursor.fetchall()

    if not rows:
        print("None")
        return

    for row in rows:
        print(
            f"{row[0]}. {row[1]} | "
            f"type={row[2]} | "
            f"udt={row[3]} | "
            f"nullable={row[4]} | "
            f"default={row[5]}"
        )


def print_constraints(cursor, table_name):
    cursor.execute(
        """
        SELECT
            constraint_data.conname,
            constraint_data.contype,
            pg_get_constraintdef(constraint_data.oid)
        FROM pg_constraint constraint_data
        JOIN pg_class table_data
          ON table_data.oid = constraint_data.conrelid
        JOIN pg_namespace namespace_data
          ON namespace_data.oid = table_data.relnamespace
        WHERE namespace_data.nspname = 'public'
          AND table_data.relname = %s
        ORDER BY constraint_data.conname
        """,
        (table_name,),
    )

    print("\nCONSTRAINTS")
    rows = cursor.fetchall()

    if not rows:
        print("None")
        return

    names = {
        "p": "PRIMARY KEY",
        "u": "UNIQUE",
        "f": "FOREIGN KEY",
        "c": "CHECK",
        "x": "EXCLUSION",
    }

    for row in rows:
        print(
            f"{row[0]} | "
            f"{names.get(row[1], row[1])} | "
            f"{row[2]}"
        )


def print_indexes(cursor, table_name):
    cursor.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = %s
        ORDER BY indexname
        """,
        (table_name,),
    )

    print("\nINDEXES")
    rows = cursor.fetchall()

    if not rows:
        print("None")
        return

    for row in rows:
        print(f"{row[0]} | {row[1]}")


def main():
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            for table_name in TABLES:
                print("\n" + "=" * 80)
                print(f"TABLE: {table_name}")
                print("=" * 80)

                print_columns(cursor, table_name)
                print_constraints(cursor, table_name)
                print_indexes(cursor, table_name)

    print("\nRISK SCHEMA INSPECTION: OK")


if __name__ == "__main__":
    main()
