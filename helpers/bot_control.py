# File: helpers/bot_control.py
# Explicit operator control for DELTAX paper trading.
#
# Usage:
#   python helpers/bot_control.py --status
#   python helpers/bot_control.py --arm
#   python helpers/bot_control.py --disarm
#   python helpers/bot_control.py --kill "reason"
#   python helpers/bot_control.py --reset-kill
#
# --arm enables new entries + execution only in PAPER mode and only if the
# kill switch is not active.

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]


def read_control(cursor):
    cursor.execute("SELECT * FROM bot_control WHERE id = 1")
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("bot_control row id=1 missing")
    return dict(row)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--arm", action="store_true")
    mode.add_argument("--disarm", action="store_true")
    mode.add_argument("--kill", metavar="REASON")
    mode.add_argument("--reset-kill", action="store_true")
    args = parser.parse_args()

    with psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            before = read_control(cursor)

            if args.status:
                print(json.dumps(before, indent=2, default=str))
                return

            if args.arm:
                if before["trading_mode"] != "paper":
                    raise RuntimeError("Refusing to arm: trading_mode is not paper")
                if before["kill_switch_active"]:
                    raise RuntimeError(
                        "Refusing to arm: kill switch is active. Reset it explicitly first."
                    )

                cursor.execute(
                    """
                    UPDATE bot_control
                    SET execution_enabled = true,
                        new_entries_enabled = true,
                        updated_at = now()
                    WHERE id = 1
                    """
                )

            elif args.disarm:
                cursor.execute(
                    """
                    UPDATE bot_control
                    SET execution_enabled = false,
                        new_entries_enabled = false,
                        updated_at = now()
                    WHERE id = 1
                    """
                )

            elif args.kill is not None:
                cursor.execute(
                    """
                    UPDATE bot_control
                    SET kill_switch_active = true,
                        kill_switch_reason = %s,
                        execution_enabled = false,
                        new_entries_enabled = false,
                        updated_at = now()
                    WHERE id = 1
                    """,
                    (args.kill,),
                )

            elif args.reset_kill:
                cursor.execute(
                    """
                    UPDATE bot_control
                    SET kill_switch_active = false,
                        kill_switch_reason = NULL,
                        execution_enabled = false,
                        new_entries_enabled = false,
                        updated_at = now()
                    WHERE id = 1
                    """
                )

            connection.commit()

            after = read_control(cursor)

    print(json.dumps({
        "before": before,
        "after": after,
    }, indent=2, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
