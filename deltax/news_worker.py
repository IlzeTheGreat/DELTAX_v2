# File: deltax/news_worker.py
# Purpose: Slow/background DELTAX news pipeline.
# Run independently from the 5-minute trading cycle.
#
# Stages:
#   market news providers -> market clustering -> market-impact AI
#   -> company news ingestion/clustering -> company-news AI
#
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

LOCK_KEY = 4200260831
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


def acquire_lock(connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
        return bool(cursor.fetchone()[0])


def release_lock(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def main():
    parser = argparse.ArgumentParser(description="DELTAX background news worker")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--market-lookback-hours", type=int, default=12)
    parser.add_argument("--company-lookback-hours", type=int, default=24)
    parser.add_argument("--market-ai-limit", type=int, default=3)
    parser.add_argument("--company-ai-limit", type=int, default=10)
    args = parser.parse_args()

    with psycopg.connect(DATABASE_URL) as lock_connection:
        if not acquire_lock(lock_connection):
            print(json.dumps({
                "status": "skipped",
                "reason": "news_worker_already_running",
            }, indent=2))
            print("NEWS WORKER: OK")
            return

        try:
            stages = []

            if args.check:
                checks = [
                    ("market_news_ingestion", [
                        "deltax/market_news_ingestion.py", "--check",
                        "--source", "all",
                        "--lookback-hours", str(args.market_lookback_hours),
                    ]),
                    ("market_event_clustering", [
                        "deltax/market_event_clustering.py", "--check",
                        "--lookback-hours", "48",
                    ]),
                    ("market_impact_ai", [
                        "deltax/market_impact_ai.py", "--check",
                        "--since-hours", "48",
                    ]),
                    ("company_news_ingestion", [
                        "deltax/company_news_ingestion.py", "--check",
                    ]),
                    ("company_news_ai", [
                        "deltax/news_ai_processor.py", "--check",
                        "--since-hours", "72",
                        "--limit", str(args.company_ai_limit),
                    ]),
                ]

                for name, command in checks:
                    result = run_stage(name, command)
                    stages.append(result)
                    if not result["ok"]:
                        break

            else:
                provider_results = []
                for provider in ("finnhub", "marketaux"):
                    result = run_stage(
                        f"market_news_ingestion_{provider}",
                        [
                            "deltax/market_news_ingestion.py", "--apply",
                            "--source", provider,
                            "--lookback-hours", str(args.market_lookback_hours),
                        ],
                    )
                    stages.append(result)
                    provider_results.append(result)

                if not any(item["ok"] for item in provider_results):
                    print(json.dumps({
                        "status": "failed",
                        "failed_stage": "market_news_providers",
                        "stages": stages,
                    }, indent=2))
                    sys.exit(1)

                pipeline = [
                    ("market_event_clustering", [
                        "deltax/market_event_clustering.py", "--apply",
                        "--lookback-hours", "48",
                        "--max-events", "100",
                    ]),
                    ("market_impact_ai", [
                        "deltax/market_impact_ai.py", "--process",
                        "--since-hours", "48",
                        "--limit", str(args.market_ai_limit),
                    ]),
                    ("company_news_ingestion", [
                        "deltax/company_news_ingestion.py", "--apply",
                        "--lookback-hours", str(args.company_lookback_hours),
                    ]),
                    ("company_news_ai", [
                        "deltax/news_ai_processor.py", "--process",
                        "--since-hours", "72",
                        "--limit", str(args.company_ai_limit),
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
                "stages": stages,
                "total_duration_seconds": round(
                    sum(item["duration_seconds"] for item in stages), 3
                ),
                "trade_intents_created": False,
                "broker_orders_submitted": False,
            }, indent=2))

            if not ok:
                sys.exit(1)

            print("NEWS WORKER: OK")
        finally:
            release_lock(lock_connection)


if __name__ == "__main__":
    main()
