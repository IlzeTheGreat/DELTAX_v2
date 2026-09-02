# File: deltax/scheduled_runner.py
# Purpose: Hidden Windows Task Scheduler launcher with persistent logs.
#
# Run with:
#   pythonw.exe deltax\\scheduled_runner.py trading
#   pythonw.exe deltax\\scheduled_runner.py news
#   pythonw.exe deltax\\scheduled_runner.py iran
#
# The actual DELTAX script is started with the project's venv python.exe,
# stdout/stderr are appended to a log file, and the child return code is
# propagated back to Windows Task Scheduler.

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

JOBS = {
    "trading": {
        "command": [
            PROJECT_ROOT / "deltax" / "trading_cycle.py",
            "--run",
        ],
        "log": PROJECT_ROOT / "logs" / "trading_cycle.log",
    },
    "news": {
        "command": [
            PROJECT_ROOT / "deltax" / "news_worker.py",
        ],
        "log": PROJECT_ROOT / "logs" / "news_worker.log",
    },
    "iran": {
        "command": [
            PROJECT_ROOT / "simulation_ir" / "deltax_event_iran_v2.py",
            "--execute",
        ],
        "log": PROJECT_ROOT / "simulation_ir" / "deltax_event_iran_v2_scheduler.log",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DELTAX hidden scheduled-task runner"
    )
    parser.add_argument("job", choices=sorted(JOBS))
    args = parser.parse_args()

    job = JOBS[args.job]
    script_and_args = [str(item) for item in job["command"]]
    log_path: Path = job["log"]

    if not VENV_PYTHON.exists():
        return 9001

    log_path.parent.mkdir(parents=True, exist_ok=True)

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW

    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        log.write(
            f"\\n[{utc_now()}] {args.job}: START "
            f"launcher_pid={os.getpid()}\\n"
        )
        log.flush()

        try:
            process = subprocess.run(
                [str(VENV_PYTHON), *script_and_args],
                cwd=PROJECT_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                creationflags=creationflags,
            )
            rc = int(process.returncode)
        except Exception as exc:
            log.write(
                f"[{utc_now()}] {args.job}: LAUNCH FAILED "
                f"{type(exc).__name__}: {exc}\\n"
            )
            log.flush()
            return 9002

        log.write(
            f"[{utc_now()}] {args.job}: END rc={rc}\\n"
        )
        log.flush()
        return rc


if __name__ == "__main__":
    sys.exit(main())
