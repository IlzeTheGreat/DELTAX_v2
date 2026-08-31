# File: deltax/scheduler.py
# DELTAX lightweight scheduler for hackathon demo / paper trading.
#
# Runs:
#   trading_cycle.py --run every 5 minutes
#   news_worker.py every 15 minutes
#
# Notes:
# - Child modules already use PostgreSQL advisory locks, so overlap is blocked.
# - This scheduler never changes bot_control.
# - Stop with Ctrl+C.
# - Logs are written under logs/.

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

TRADING_INTERVAL = 5 * 60
NEWS_INTERVAL = 15 * 60


def log_line(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def run_child(name: str, args: list[str]) -> int:
    started = datetime.now(timezone.utc)
    log_path = LOG_DIR / f"{name}.log"

    command = [
        sys.executable,
        str(PROJECT_ROOT / args[0]),
        *args[1:],
    ]

    log_line(
        log_path,
        f"\n===== {started.isoformat()} START {' '.join(command)} =====",
    )

    process = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    if process.stdout:
        log_line(log_path, process.stdout)
    if process.stderr:
        log_line(log_path, "STDERR:\n" + process.stderr)

    finished = datetime.now(timezone.utc)
    log_line(
        log_path,
        f"===== {finished.isoformat()} END rc={process.returncode} =====",
    )

    print(
        f"[{finished.isoformat()}] {name}: "
        f"{'OK' if process.returncode == 0 else 'FAILED'}"
    )

    return process.returncode


def sleep_until(target_monotonic: float) -> None:
    while True:
        remaining = target_monotonic - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 1.0))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run DELTAX trading/news loops."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one news cycle and one trading cycle, then exit.",
    )
    args = parser.parse_args()

    if args.once:
        run_child("news_worker", ["deltax/news_worker.py"])
        run_child(
            "trading_cycle",
            ["deltax/trading_cycle.py", "--run"],
        )
        return

    print("DELTAX scheduler started.")
    print("Trading cycle: every 5 minutes.")
    print("News worker: every 15 minutes.")
    print("Ctrl+C to stop.")

    now = time.monotonic()
    next_trading = now
    next_news = now

    try:
        while True:
            current = time.monotonic()

            if current >= next_news:
                run_child(
                    "news_worker",
                    ["deltax/news_worker.py"],
                )
                while next_news <= time.monotonic():
                    next_news += NEWS_INTERVAL

            current = time.monotonic()

            if current >= next_trading:
                run_child(
                    "trading_cycle",
                    ["deltax/trading_cycle.py", "--run"],
                )
                while next_trading <= time.monotonic():
                    next_trading += TRADING_INTERVAL

            target = min(next_news, next_trading)
            sleep_until(target)

    except KeyboardInterrupt:
        print("\nDELTAX scheduler stopped.")


if __name__ == "__main__":
    main()
