# File: helpers/inspect_news_ai_schema.py
# Purpose: Prints the exact database structure required for persisting news clusters, AI analyses, and trade theses.

import os

import psycopg
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

TABLES = [
    "event_clusters",
    "event_cluster_members",
    "ai_analyses",
    "trade_theses",
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

    for row in cursor.fetchall():
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

    print("\nNEWS AND AI SCHEMA INSPECTION: OK")


if __name__ == "__main__":
    main()