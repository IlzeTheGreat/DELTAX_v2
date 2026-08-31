# File: helpers/inspect_strategy_config.py
# Purpose: Prints the strategy_configs schema and existing configuration records without modifying the database.

import json
import os

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


def main():
    with psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
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
                  AND table_name = 'strategy_configs'
                ORDER BY ordinal_position
                """
            )

            columns = cursor.fetchall()

            print("STRATEGY_CONFIGS COLUMNS")

            for column in columns:
                print(
                    f"{column['ordinal_position']}. "
                    f"{column['column_name']} | "
                    f"type={column['data_type']} | "
                    f"nullable={column['is_nullable']} | "
                    f"default={column['column_default']}"
                )

            cursor.execute(
                """
                SELECT
                    constraint_data.conname AS constraint_name,
                    constraint_data.contype AS constraint_type,
                    pg_get_constraintdef(
                        constraint_data.oid
                    ) AS definition
                FROM pg_constraint constraint_data
                JOIN pg_class table_data
                    ON table_data.oid =
                       constraint_data.conrelid
                JOIN pg_namespace namespace_data
                    ON namespace_data.oid =
                       table_data.relnamespace
                WHERE namespace_data.nspname = 'public'
                  AND table_data.relname = 'strategy_configs'
                ORDER BY constraint_data.conname
                """
            )

            constraints = cursor.fetchall()

            print("\nSTRATEGY_CONFIGS CONSTRAINTS")

            for constraint in constraints:
                print(
                    f"{constraint['constraint_name']} | "
                    f"type={constraint['constraint_type']} | "
                    f"{constraint['definition']}"
                )

            cursor.execute(
                """
                SELECT *
                FROM strategy_configs
                """
            )

            records = cursor.fetchall()

            print(
                f"\nSTRATEGY_CONFIGS RECORDS: "
                f"{len(records)}"
            )

            for index, record in enumerate(records, start=1):
                print(f"\nRECORD {index}")
                print(
                    json.dumps(
                        record,
                        indent=2,
                        default=str,
                        ensure_ascii=False,
                    )
                )

    print("\nNo database changes were made.")
    print("STRATEGY CONFIG INSPECTION: OK")


if __name__ == "__main__":
    main()