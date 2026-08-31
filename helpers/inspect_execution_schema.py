# File: helpers/inspect_execution_schema.py
# Purpose: Prints the database structure needed before implementing
# DELTAX trade-intent, risk-gate, execution, and reconciliation layers.

import os

import psycopg
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

TABLES = [
    "trade_intents",
    "broker_orders",
    "trade_theses",
    "strategy_configs",
    "positions",
    "position_states",
    "fills",
    "trades",
    "strategy_cash",
    "portfolio_snapshots",
]


def table_exists(cursor, table_name):
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        )
        """,
        (table_name,),
    )
    return bool(cursor.fetchone()[0])


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

    type_names = {
        "p": "PRIMARY KEY",
        "u": "UNIQUE",
        "f": "FOREIGN KEY",
        "c": "CHECK",
        "x": "EXCLUSION",
    }

    for row in rows:
        print(
            f"{row[0]} | "
            f"{type_names.get(row[1], row[1])} | "
            f"{row[2]}"
        )


def print_indexes(cursor, table_name):
    cursor.execute(
        """
        SELECT
            indexname,
            indexdef
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


def print_public_tables(cursor):
    cursor.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )

    print("\n" + "=" * 80)
    print("ALL PUBLIC TABLES")
    print("=" * 80)

    for row in cursor.fetchall():
        print(row[0])


def main():
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            print_public_tables(cursor)

            for table_name in TABLES:
                print("\n" + "=" * 80)
                print(f"TABLE: {table_name}")
                print("=" * 80)

                if not table_exists(cursor, table_name):
                    print("TABLE DOES NOT EXIST")
                    continue

                print_columns(cursor, table_name)
                print_constraints(cursor, table_name)
                print_indexes(cursor, table_name)

    print("\nEXECUTION SCHEMA INSPECTION: OK")


if __name__ == "__main__":
    main()
