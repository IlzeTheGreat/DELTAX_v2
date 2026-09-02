# File: deltax/company_news_worker.py
# Purpose: DELTAX company-specific news ingestion + AI classification.
# Runs separately from market-news processing so slow company AI cannot delay
# ETF/market-regime updates.
# No trade intents or broker orders are created here.

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Separate advisory lock from market_news_worker.py.
LOCK_KEY = 4200260902
MAX_OUTPUT_CHARS = 12000


def compact(value: str) -> str:
    value = value.strip()
    if len(value) <= MAX_OUTPUT_CHARS:
        return value
    return "...[truncated]...\n" + value[-MAX_OUTPUT_CHARS:]


def run_stage(name: str, args: list[str]) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()

    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"

    process = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / args[0]), *args[1:]],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
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


def acquire_lock(connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
        return bool(cursor.fetchone()[0])


def release_lock(connection) -> None:
    try:
        if connection.closed:
            return
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    except psycopg.Error as exc:
        print(f"COMPANY NEWS WORKER LOCK RELEASE WARNING: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="DELTAX company-news worker")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--ai-since-hours", type=int, default=72)
    # Keep this deliberately below the old 10. Company AI is the slow stage.
    parser.add_argument("--ai-limit", type=int, default=5)
    args = parser.parse_args()

    with psycopg.connect(DATABASE_URL, autocommit=True) as lock_connection:
        if not acquire_lock(lock_connection):
            print(json.dumps({
                "status": "skipped",
                "reason": "company_news_worker_already_running",
            }, indent=2))
            print("COMPANY NEWS WORKER: OK")
            return

        try:
            stages: list[dict[str, Any]] = []

            if args.check:
                pipeline = [
                    ("company_news_ingestion", [
                        "deltax/company_news_ingestion.py", "--check",
                    ]),
                    ("company_news_ai", [
                        "deltax/news_ai_processor.py", "--check",
                        "--since-hours", str(args.ai_since_hours),
                        "--limit", str(args.ai_limit),
                    ]),
                ]
            else:
                pipeline = [
                    ("company_news_ingestion", [
                        "deltax/company_news_ingestion.py", "--apply",
                        "--lookback-hours", str(args.lookback_hours),
                    ]),
                    ("company_news_ai", [
                        "deltax/news_ai_processor.py", "--process",
                        "--since-hours", str(args.ai_since_hours),
                        "--limit", str(args.ai_limit),
                    ]),
                ]

            for name, command in pipeline:
                result = run_stage(name, command)
                stages.append(result)
                if not result["ok"]:
                    break

            ok = all(item["ok"] for item in stages)
            print(json.dumps({
                "status": "ok" if ok else "failed",
                "mode": "check" if args.check else "run",
                "worker": "company_news",
                "stages": stages,
                "total_duration_seconds": round(
                    sum(item["duration_seconds"] for item in stages), 3
                ),
                "trade_intents_created": False,
                "broker_orders_submitted": False,
            }, indent=2))

            if not ok:
                sys.exit(1)

            print("COMPANY NEWS WORKER: OK")
        finally:
            release_lock(lock_connection)


if __name__ == "__main__":
    main()
