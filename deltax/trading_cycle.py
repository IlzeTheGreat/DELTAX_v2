# File: deltax/trading_cycle.py
# Purpose: Fast 5-minute DELTAX trading loop.
#
# The slow news pipeline runs separately in news_worker.py.
# This loop reads the latest persisted news/AI context from Neon.
# If fresh news is not yet analyzed, the existing router fails closed.
#
# Stages:
#   pre-reconcile -> portfolio risk sync -> exit intents
#   -> candidate-only news refresh -> scan/router
#   -> stock intents -> option intents -> paper executor -> post-reconcile
#
# Safety:
# - advisory lock prevents overlapping 5-minute cycles
# - never overrides bot_control
# - execution happens only when explicitly armed in bot_control

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
PROJECT_ROOT = Path(__file__).resolve().parents[1]

LOCK_KEY = 4200260832
LOCK_KEEPALIVE_SECONDS = 30
MAX_OUTPUT_CHARS = 12000


def compact(value: str) -> str:
    value = value.strip()
    if len(value) <= MAX_OUTPUT_CHARS:
        return value
    return "...[truncated]...\n" + value[-MAX_OUTPUT_CHARS:]


def run_stage(name: str, args: list[str]) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    process = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / args[0]), *args[1:]],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "stage": name,
        "ok": process.returncode == 0,
        "returncode": process.returncode,
        "duration_seconds": round(time.monotonic() - t0, 3),
        "started_at": started.isoformat(),
        "stdout": compact(process.stdout),
        "stderr": compact(process.stderr),
    }


def load_control() -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT
                    trading_mode,
                    execution_enabled,
                    new_entries_enabled,
                    kill_switch_active,
                    kill_switch_reason
                FROM bot_control
                WHERE id = 1
            """)
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("bot_control row id=1 is missing")
    return dict(row)


def heartbeat() -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE bot_control
                SET last_heartbeat_at = now(),
                    updated_at = now()
                WHERE id = 1
                """
            )
        connection.commit()


def acquire_lock(connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
        return bool(cursor.fetchone()[0])


def release_lock(connection) -> None:
    # Advisory locks are session-level. If the DB connection has already been
    # closed/terminated, PostgreSQL has released the lock automatically.
    if connection.closed:
        return

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    except psycopg.Error as exc:
        # Do not convert an otherwise successful trading cycle into rc=1 only
        # because cleanup failed. Closing the session releases the advisory lock.
        print(
            f"WARNING: failed to explicitly release trading-cycle advisory lock: {exc}",
            file=sys.stderr,
        )


def keep_lock_connection_alive(connection, stop_event: threading.Event) -> None:
    """
    Keep the PostgreSQL session that owns the advisory lock active.

    Some hosted PostgreSQL services terminate sessions that stay idle for
    several minutes. Because DELTAX stages can run longer than five minutes,
    losing this session would also release the advisory lock too early and
    allow overlapping trading cycles.

    The keepalive query runs in autocommit mode and does not alter application
    data or the advisory lock.
    """
    while not stop_event.wait(LOCK_KEEPALIVE_SECONDS):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except psycopg.Error as exc:
            print(
                f"WARNING: trading-cycle lock keepalive failed: {exc}",
                file=sys.stderr,
            )
            return


def main():
    parser = argparse.ArgumentParser(description="DELTAX fast trading cycle")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--execution-limit", type=int, default=5)
    args = parser.parse_args()

    with psycopg.connect(
        DATABASE_URL,
        autocommit=True,
    ) as lock_connection:
        if not acquire_lock(lock_connection):
            print(json.dumps({
                "status": "skipped",
                "reason": "trading_cycle_already_running",
            }, indent=2))
            print("TRADING CYCLE: OK")
            return

        lock_keepalive_stop = threading.Event()
        lock_keepalive_thread = threading.Thread(
            target=keep_lock_connection_alive,
            args=(lock_connection, lock_keepalive_stop),
            name="deltax-trading-cycle-lock-keepalive",
            daemon=True,
        )
        lock_keepalive_thread.start()

        try:
            control = load_control()
            stages = []

            if args.check:
                checks = [
                    ("broker_order_reconciler_pre", [
                        "deltax/broker_order_reconciler.py", "--check"
                    ]),
                    ("portfolio_risk_monitor", [
                        "deltax/portfolio_risk_monitor.py", "--check"
                    ]),
                    ("exit_intent_builder", [
                        "deltax/exit_intent_builder.py", "--check"
                    ]),
                    ("candidate_news_refresh", [
                        "deltax/candidate_news_refresh.py", "--check"
                    ]),
                    ("scan_cycle", ["deltax/scan_cycle.py", "--check"]),
                    ("stock_trade_intent_builder", [
                        "deltax/stock_trade_intent_builder.py", "--check"
                    ]),
                    ("options_spread_intent_builder", [
                        "deltax/options_spread_intent_builder.py", "--check"
                    ]),
                    ("paper_executor", ["deltax/paper_executor.py", "--check"]),
                ]
                for name, command in checks:
                    result = run_stage(name, command)
                    stages.append(result)
                    if not result["ok"]:
                        break
            else:
                pre_reconcile = run_stage(
                    "broker_order_reconciler_pre",
                    [
                        "deltax/broker_order_reconciler.py",
                        "--sync",
                        "--limit", "50",
                    ],
                )
                stages.append(pre_reconcile)

                if not pre_reconcile["ok"]:
                    heartbeat()
                    print(json.dumps({
                        "status": "failed",
                        "mode": "run",
                        "failed_stage": "broker_order_reconciler_pre",
                        "bot_control": load_control(),
                        "stages": stages,
                    }, indent=2))
                    sys.exit(1)

                portfolio_risk = run_stage(
                    "portfolio_risk_monitor",
                    [
                        "deltax/portfolio_risk_monitor.py",
                        "--sync",
                    ],
                )
                stages.append(portfolio_risk)

                if not portfolio_risk["ok"]:
                    heartbeat()
                    print(json.dumps({
                        "status": "failed",
                        "mode": "run",
                        "failed_stage": "portfolio_risk_monitor",
                        "bot_control": load_control(),
                        "stages": stages,
                    }, indent=2))
                    sys.exit(1)

                exit_intents = run_stage(
                    "exit_intent_builder",
                    [
                        "deltax/exit_intent_builder.py",
                        "--process",
                        "--limit", "20",
                    ],
                )
                stages.append(exit_intents)

                if not exit_intents["ok"]:
                    heartbeat()
                    print(json.dumps({
                        "status": "failed",
                        "mode": "run",
                        "failed_stage": "exit_intent_builder",
                        "bot_control": load_control(),
                        "stages": stages,
                    }, indent=2))
                    sys.exit(1)

                candidate_news = run_stage(
                    "candidate_news_refresh",
                    [
                        "deltax/candidate_news_refresh.py",
                        "--run",
                        "--lookback-hours", "24",
                        "--ai-since-hours", "72",
                        "--ai-limit", "5",
                    ],
                )
                stages.append(candidate_news)

                if not candidate_news["ok"]:
                    heartbeat()
                    print(json.dumps({
                        "status": "failed",
                        "mode": "run",
                        "failed_stage": "candidate_news_refresh",
                        "bot_control": load_control(),
                        "stages": stages,
                    }, indent=2))
                    sys.exit(1)

                result = run_stage(
                    "scan_cycle",
                    ["deltax/scan_cycle.py", "--run"],
                )
                stages.append(result)

                if result["ok"]:
                    control = load_control()

                    if control["kill_switch_active"]:
                        stages.append({
                            "stage": "entry_gate",
                            "ok": True,
                            "returncode": 0,
                            "duration_seconds": 0,
                            "started_at": datetime.now(timezone.utc).isoformat(),
                            "stdout": "SKIPPED: kill_switch_active=true",
                            "stderr": "",
                        })
                    elif not control["new_entries_enabled"]:
                        stages.append({
                            "stage": "entry_intent_builders",
                            "ok": True,
                            "returncode": 0,
                            "duration_seconds": 0,
                            "started_at": datetime.now(timezone.utc).isoformat(),
                            "stdout": "SKIPPED: new_entries_enabled=false",
                            "stderr": "",
                        })

                        # Exit and emergency-exit intents are still allowed to
                        # flow to the executor even when new entries are blocked.
                        stages.append(
                            run_stage(
                                "paper_executor",
                                [
                                    "deltax/paper_executor.py",
                                    "--execute",
                                    "--limit", str(args.execution_limit),
                                ],
                            )
                        )
                    else:
                        for name, command in [
                            ("stock_trade_intent_builder", [
                                "deltax/stock_trade_intent_builder.py",
                                "--process", "--limit", "20",
                            ]),
                            ("options_spread_intent_builder", [
                                "deltax/options_spread_intent_builder.py",
                                "--process", "--limit", "10",
                            ]),
                        ]:
                            stage = run_stage(name, command)
                            stages.append(stage)
                            if not stage["ok"]:
                                break

                        control = load_control()
                        if (
                            all(item["ok"] for item in stages)
                            and control["trading_mode"] == "paper"
                            and control["execution_enabled"]
                            and control["new_entries_enabled"]
                            and not control["kill_switch_active"]
                        ):
                            stages.append(run_stage(
                                "paper_executor",
                                [
                                    "deltax/paper_executor.py",
                                    "--execute",
                                    "--limit", str(args.execution_limit),
                                ],
                            ))
                        else:
                            stages.append({
                                "stage": "paper_executor",
                                "ok": True,
                                "returncode": 0,
                                "duration_seconds": 0,
                                "started_at": datetime.now(timezone.utc).isoformat(),
                                "stdout": "SKIPPED: execution not armed",
                                "stderr": "",
                            })

                # Always reconcile again after the cycle. If execution submitted
                # an order and Alpaca filled it immediately, this second pass
                # materializes the fill/position without waiting five minutes.
                if all(item["ok"] for item in stages):
                    stages.append(
                        run_stage(
                            "broker_order_reconciler_post",
                            [
                                "deltax/broker_order_reconciler.py",
                                "--sync",
                                "--limit", "50",
                            ],
                        )
                    )

            heartbeat()
            ok = all(item["ok"] for item in stages)

            print(json.dumps({
                "status": "ok" if ok else "failed",
                "mode": "check" if args.check else "run",
                "bot_control": load_control(),
                "stages": stages,
                "total_duration_seconds": round(
                    sum(item["duration_seconds"] for item in stages), 3
                ),
                "news_pipeline": "external_news_worker",
                "fresh_unprocessed_news_behavior": "router_fails_closed",
            }, indent=2))

            if not ok:
                sys.exit(1)

            print("TRADING CYCLE: OK")
        finally:
            lock_keepalive_stop.set()
            lock_keepalive_thread.join(timeout=5)
            release_lock(lock_connection)


if __name__ == "__main__":
    main()
