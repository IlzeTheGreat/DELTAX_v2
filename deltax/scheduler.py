# File: deltax/scheduler.py
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


class Job:
    def __init__(self, name: str, args: list[str], interval: int):
        self.name = name
        self.args = args
        self.interval = interval
        self.next_run = time.monotonic()
        self.process: subprocess.Popen | None = None
        self.log_handle = None

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.name}.log"

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def reap(self) -> None:
        if self.process is None:
            return
        return_code = self.process.poll()
        if return_code is None:
            return

        finished = datetime.now(timezone.utc)
        if self.log_handle:
            self.log_handle.write(
                f"\n===== {finished.isoformat()} END rc={return_code} =====\n"
            )
            self.log_handle.flush()
            self.log_handle.close()
            self.log_handle = None

        print(
            f"[{finished.isoformat()}] {self.name}: "
            f"{'OK' if return_code == 0 else 'FAILED'}"
        )
        self.process = None

    def advance_schedule(self) -> None:
        current = time.monotonic()
        while self.next_run <= current:
            self.next_run += self.interval

    def start_if_due(self) -> None:
        self.reap()
        if time.monotonic() < self.next_run:
            return

        if self.running():
            print(
                f"[{datetime.now(timezone.utc).isoformat()}] "
                f"{self.name}: SKIPPED, previous run still active"
            )
            self.advance_schedule()
            return

        started = datetime.now(timezone.utc)
        command = [
            sys.executable,
            str(PROJECT_ROOT / self.args[0]),
            *self.args[1:],
        ]

        self.log_handle = self.log_path.open(
            "a",
            encoding="utf-8",
            buffering=1,
        )
        self.log_handle.write(
            f"\n===== {started.isoformat()} START {' '.join(command)} =====\n"
        )
        self.log_handle.flush()

        self.process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

        print(
            f"[{started.isoformat()}] "
            f"{self.name}: START pid={self.process.pid}"
        )
        self.advance_schedule()

    def stop(self) -> None:
        if self.running():
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reap()
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run independent DELTAX trading/news loops."
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    trading = Job(
        "trading_cycle",
        ["deltax/trading_cycle.py", "--run"],
        TRADING_INTERVAL,
    )
    news = Job(
        "news_worker",
        ["deltax/news_worker.py"],
        NEWS_INTERVAL,
    )
    jobs = [trading, news]

    if args.once:
        for job in jobs:
            job.start_if_due()
        while any(job.running() for job in jobs):
            for job in jobs:
                job.reap()
            time.sleep(0.5)
        return

    print("DELTAX scheduler v2 started.")
    print("Trading cycle: every 5 minutes, independent.")
    print("News worker: every 15 minutes, independent.")
    print(f"Logs: {LOG_DIR}")
    print("Ctrl+C to stop.")

    try:
        while True:
            for job in jobs:
                job.start_if_due()
                job.reap()
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nStopping DELTAX scheduler...")

    finally:
        for job in jobs:
            job.stop()
        print("DELTAX scheduler stopped.")


if __name__ == "__main__":
    main()
