# File: deltax/market_news_worker.py
# Purpose: Fast/shared DELTAX market-news pipeline for stock/options + ETF bots.
# Runs independently from company-specific news processing.
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

# Separate lock from company worker so both pipelines may run simultaneously.
LOCK_KEY = 4200260901
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
    # Windows Scheduled Tasks may inherit cp1252/charmap. Force UTF-8 because
    # financial news routinely contains Unicode punctuation.
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
        print(f"MARKET NEWS WORKER LOCK RELEASE WARNING: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="DELTAX market-news worker")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--lookback-hours", type=int, default=12)
    parser.add_argument("--ai-since-hours", type=int, default=48)
    parser.add_argument("--ai-limit", type=int, default=10)
    parser.add_argument("--max-events", type=int, default=100)
    args = parser.parse_args()

    with psycopg.connect(DATABASE_URL, autocommit=True) as lock_connection:
        if not acquire_lock(lock_connection):
            print(json.dumps({
                "status": "skipped",
                "reason": "market_news_worker_already_running",
            }, indent=2))
            print("MARKET NEWS WORKER: OK")
            return

        try:
            stages: list[dict[str, Any]] = []

            if args.check:
                checks = [
                    ("market_news_ingestion", [
                        "deltax/market_news_ingestion.py", "--check",
                        "--source", "all",
                        "--lookback-hours", str(args.lookback_hours),
                    ]),
                    ("market_event_clustering", [
                        "deltax/market_event_clustering.py", "--check",
                        "--lookback-hours", str(args.ai_since_hours),
                    ]),
                    ("market_impact_ai", [
                        "deltax/market_impact_ai.py", "--check",
                        "--since-hours", str(args.ai_since_hours),
                    ]),
                ]
                for name, command in checks:
                    result = run_stage(name, command)
                    stages.append(result)
                    if not result["ok"]:
                        break
            else:
                provider_results = []

                # Alpaca is first because it is the freshest/most directly useful
                # source for our managed universe and ETF layer.
                for provider in ("alpaca", "finnhub", "marketaux"):
                    result = run_stage(
                        f"market_news_ingestion_{provider}",
                        [
                            "deltax/market_news_ingestion.py", "--apply",
                            "--source", provider,
                            "--lookback-hours", str(args.lookback_hours),
                        ],
                    )
                    stages.append(result)
                    provider_results.append(result)

                # Continue if at least one provider works. One flaky provider must
                # not block the whole market-intelligence pipeline.
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
                        "--lookback-hours", str(args.ai_since_hours),
                        "--max-events", str(args.max_events),
                    ]),
                    ("market_impact_ai", [
                        "deltax/market_impact_ai.py", "--process",
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
                "worker": "market_news",
                "stages": stages,
                "total_duration_seconds": round(
                    sum(item["duration_seconds"] for item in stages), 3
                ),
                "trade_intents_created": False,
                "broker_orders_submitted": False,
            }, indent=2))

            if not ok:
                sys.exit(1)

            print("MARKET NEWS WORKER: OK")
        finally:
            release_lock(lock_connection)


if __name__ == "__main__":
    main()
