# File: deltax/agent_cycle.py
# Purpose: One top-level DELTAX autonomous cycle.
#
# Current production stages:
#   1) market-risk news ingestion
#   2) market-news clustering
#   3) market-impact AI
#   4) company-news ingestion + clustering
#   5) company-news AI
#   6) technical scan + direction routing + confirmation persistence
#   7) stock intent builder
#   8) options spread intent builder
#   9) Alpaca PAPER executor
#
# Safety:
# - --check is read-only.
# - --run never overrides bot_control.
# - Intent builders are skipped while new_entries_enabled=false.
# - Executor is skipped while execution_enabled=false.
# - paper_executor itself also fails closed.
#
# Company-news refresh ingestion is productionized through
# deltax/company_news_ingestion.py.

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
from psycopg.rows import dict_row


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DELTAX_DIR = PROJECT_ROOT / "deltax"

DEFAULT_MARKET_LOOKBACK_HOURS = 12
DEFAULT_MARKET_CLUSTER_LOOKBACK_HOURS = 48
DEFAULT_MARKET_AI_SINCE_HOURS = 48
DEFAULT_COMPANY_NEWS_LOOKBACK_HOURS = 24
DEFAULT_COMPANY_AI_SINCE_HOURS = 72
DEFAULT_MARKET_AI_LIMIT = 3
DEFAULT_COMPANY_AI_LIMIT = 10
DEFAULT_EXECUTION_LIMIT = 5

MAX_OUTPUT_CHARS = 12000


def json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def compact_text(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return "...[truncated]...\n" + value[-limit:]


def run_command(name: str, command: list[str]) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()

    process = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    finished = datetime.now(timezone.utc)

    return {
        "stage": name,
        "command": command,
        "returncode": process.returncode,
        "ok": process.returncode == 0,
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": round(
            time.monotonic() - started_monotonic,
            3,
        ),
        "stdout": compact_text(process.stdout),
        "stderr": compact_text(process.stderr),
    }


def python_script(relative_path: str, *args: str) -> list[str]:
    return [
        sys.executable,
        str(PROJECT_ROOT / relative_path),
        *args,
    ]


def load_bot_control() -> dict[str, Any]:
    with psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    trading_mode,
                    execution_enabled,
                    new_entries_enabled,
                    kill_switch_active,
                    kill_switch_reason,
                    last_heartbeat_at,
                    updated_at
                FROM bot_control
                WHERE id = 1
                """
            )
            row = cursor.fetchone()

    if row is None:
        raise RuntimeError("bot_control row id=1 is missing")

    return dict(row)


def update_heartbeat() -> None:
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE bot_control
                SET last_heartbeat_at = now()
                WHERE id = 1
                """
            )
        connection.commit()


def stage_summary(stage: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": stage["stage"],
        "ok": stage["ok"],
        "returncode": stage["returncode"],
        "duration_seconds": stage["duration_seconds"],
    }


class AgentCycle:
    def __init__(
        self,
        market_lookback_hours: int,
        market_cluster_lookback_hours: int,
        market_ai_since_hours: int,
        company_news_lookback_hours: int,
        company_ai_since_hours: int,
        market_ai_limit: int,
        company_ai_limit: int,
        execution_limit: int,
    ):
        self.market_lookback_hours = market_lookback_hours
        self.market_cluster_lookback_hours = (
            market_cluster_lookback_hours
        )
        self.market_ai_since_hours = market_ai_since_hours
        self.company_news_lookback_hours = company_news_lookback_hours
        self.company_ai_since_hours = company_ai_since_hours
        self.market_ai_limit = market_ai_limit
        self.company_ai_limit = company_ai_limit
        self.execution_limit = execution_limit

    def check_commands(self) -> list[tuple[str, list[str]]]:
        return [
            (
                "market_news_ingestion",
                python_script(
                    "deltax/market_news_ingestion.py",
                    "--check",
                    "--source",
                    "all",
                    "--lookback-hours",
                    str(self.market_lookback_hours),
                ),
            ),
            (
                "market_event_clustering",
                python_script(
                    "deltax/market_event_clustering.py",
                    "--check",
                    "--lookback-hours",
                    str(self.market_cluster_lookback_hours),
                ),
            ),
            (
                "market_impact_ai",
                python_script(
                    "deltax/market_impact_ai.py",
                    "--check",
                    "--since-hours",
                    str(self.market_ai_since_hours),
                ),
            ),
            (
                "company_news_ingestion",
                python_script(
                    "deltax/company_news_ingestion.py",
                    "--check",
                ),
            ),
            (
                "company_news_ai",
                python_script(
                    "deltax/news_ai_processor.py",
                    "--check",
                    "--since-hours",
                    str(self.company_ai_since_hours),
                    "--limit",
                    str(self.company_ai_limit),
                ),
            ),
            (
                "scan_cycle",
                python_script(
                    "deltax/scan_cycle.py",
                    "--check",
                ),
            ),
            (
                "stock_trade_intent_builder",
                python_script(
                    "deltax/stock_trade_intent_builder.py",
                    "--check",
                ),
            ),
            (
                "options_spread_intent_builder",
                python_script(
                    "deltax/options_spread_intent_builder.py",
                    "--check",
                ),
            ),
            (
                "paper_executor",
                python_script(
                    "deltax/paper_executor.py",
                    "--check",
                ),
            ),
        ]

    def health_check(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        control = load_bot_control()

        stages = []
        for name, command in self.check_commands():
            result = run_command(name, command)
            stages.append(result)

            if not result["ok"]:
                break

        all_ok = (
            len(stages) == len(self.check_commands())
            and all(stage["ok"] for stage in stages)
        )

        return {
            "status": "ok" if all_ok else "failed",
            "mode": "check",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc),
            "bot_control": control,
            "stages": stages,
            "summary": [stage_summary(stage) for stage in stages],
            "company_news_refresh_ingestion_productionized": True,
            "company_news_ai_on_persisted_clusters": True,
            "market_news_provider_failover": True,
            "writes_performed_by_agent_cycle": False,
            "broker_orders_submitted_by_agent_cycle": False,
        }

    def run_stage(
        self,
        stages: list[dict[str, Any]],
        name: str,
        command: list[str],
        stop_on_failure: bool = True,
    ) -> bool:
        result = run_command(name, command)
        stages.append(result)

        if not result["ok"] and stop_on_failure:
            return False

        return True

    def run(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        stages: list[dict[str, Any]] = []

        control_before = load_bot_control()

        # 1. Market-risk news ingestion.
        #
        # Finnhub and Marketaux are independent upstream providers. A temporary
        # failure in one provider must not kill the whole trading cycle if the
        # other provider is still available. We therefore call them separately
        # and continue in degraded mode when at least one succeeds.
        market_provider_results = []

        for provider in ("finnhub", "marketaux"):
            result = run_command(
                f"market_news_ingestion_{provider}",
                python_script(
                    "deltax/market_news_ingestion.py",
                    "--apply",
                    "--source",
                    provider,
                    "--lookback-hours",
                    str(self.market_lookback_hours),
                ),
            )
            stages.append(result)
            market_provider_results.append(result)

        successful_market_providers = [
            result["stage"]
            for result in market_provider_results
            if result["ok"]
        ]
        failed_market_providers = [
            {
                "stage": result["stage"],
                "stderr": result["stderr"],
            }
            for result in market_provider_results
            if not result["ok"]
        ]

        if not successful_market_providers:
            return self.failed_result(
                started,
                stages,
                control_before,
            )

        stages.append(
            {
                "stage": "market_news_provider_gate",
                "ok": True,
                "returncode": 0,
                "duration_seconds": 0,
                "stdout": json.dumps(
                    {
                        "mode": (
                            "normal"
                            if not failed_market_providers
                            else "degraded"
                        ),
                        "successful_providers":
                            successful_market_providers,
                        "failed_providers":
                            failed_market_providers,
                        "decision":
                            "continue_at_least_one_market_news_provider_available",
                    },
                    ensure_ascii=False,
                ),
                "stderr": "",
            }
        )

        # 2. Persist market-event clusters.
        if not self.run_stage(
            stages,
            "market_event_clustering",
            python_script(
                "deltax/market_event_clustering.py",
                "--apply",
                "--lookback-hours",
                str(self.market_cluster_lookback_hours),
                "--max-events",
                "100",
            ),
        ):
            return self.failed_result(
                started,
                stages,
                control_before,
            )

        # 3. AI classification of market clusters.
        if not self.run_stage(
            stages,
            "market_impact_ai",
            python_script(
                "deltax/market_impact_ai.py",
                "--process",
                "--since-hours",
                str(self.market_ai_since_hours),
                "--limit",
                str(self.market_ai_limit),
            ),
        ):
            return self.failed_result(
                started,
                stages,
                control_before,
            )

        # 4. Refresh Alpaca company news and rebuild deterministic clusters.
        if not self.run_stage(
            stages,
            "company_news_ingestion",
            python_script(
                "deltax/company_news_ingestion.py",
                "--apply",
                "--lookback-hours",
                str(self.company_news_lookback_hours),
            ),
        ):
            return self.failed_result(
                started,
                stages,
                control_before,
            )

        # 5. AI classification of persisted company-news clusters.
        if not self.run_stage(
            stages,
            "company_news_ai",
            python_script(
                "deltax/news_ai_processor.py",
                "--process",
                "--since-hours",
                str(self.company_ai_since_hours),
                "--limit",
                str(self.company_ai_limit),
            ),
        ):
            return self.failed_result(
                started,
                stages,
                control_before,
            )

        # 6. Technical scanner + router + confirmations.
        if not self.run_stage(
            stages,
            "scan_cycle",
            python_script(
                "deltax/scan_cycle.py",
                "--run",
            ),
        ):
            return self.failed_result(
                started,
                stages,
                control_before,
            )

        # Reload controls in case a human/operator changed them during cycle.
        control_after_scan = load_bot_control()

        if control_after_scan["kill_switch_active"]:
            stages.append(
                {
                    "stage": "entry_and_execution_gate",
                    "ok": True,
                    "returncode": 0,
                    "duration_seconds": 0,
                    "stdout": "SKIPPED: kill_switch_active=true",
                    "stderr": "",
                }
            )
            update_heartbeat()
            return self.completed_result(
                started,
                stages,
                control_before,
                control_after_scan,
                intents_skipped=True,
                execution_skipped=True,
            )

        # Intent builders are not run while entries are disabled. This avoids
        # generating repetitive risk_events every five minutes.
        if not control_after_scan["new_entries_enabled"]:
            stages.append(
                {
                    "stage": "intent_builders",
                    "ok": True,
                    "returncode": 0,
                    "duration_seconds": 0,
                    "stdout": (
                        "SKIPPED: new_entries_enabled=false. "
                        "Approved theses remain available for later processing."
                    ),
                    "stderr": "",
                }
            )

            execution_skipped = True

        else:
            # 7. Stock intents.
            if not self.run_stage(
                stages,
                "stock_trade_intent_builder",
                python_script(
                    "deltax/stock_trade_intent_builder.py",
                    "--process",
                    "--limit",
                    "20",
                ),
            ):
                return self.failed_result(
                    started,
                    stages,
                    control_before,
                )

            # 8. Option-spread intents.
            if not self.run_stage(
                stages,
                "options_spread_intent_builder",
                python_script(
                    "deltax/options_spread_intent_builder.py",
                    "--process",
                    "--limit",
                    "10",
                ),
            ):
                return self.failed_result(
                    started,
                    stages,
                    control_before,
                )

            control_before_execution = load_bot_control()

            # 9. Paper execution only when explicitly armed.
            if (
                control_before_execution["execution_enabled"]
                and control_before_execution["new_entries_enabled"]
                and not control_before_execution["kill_switch_active"]
                and control_before_execution["trading_mode"] == "paper"
            ):
                if not self.run_stage(
                    stages,
                    "paper_executor",
                    python_script(
                        "deltax/paper_executor.py",
                        "--execute",
                        "--limit",
                        str(self.execution_limit),
                    ),
                ):
                    return self.failed_result(
                        started,
                        stages,
                        control_before,
                    )

                execution_skipped = False

            else:
                stages.append(
                    {
                        "stage": "paper_executor",
                        "ok": True,
                        "returncode": 0,
                        "duration_seconds": 0,
                        "stdout": (
                            "SKIPPED: execution is not armed by bot_control."
                        ),
                        "stderr": "",
                    }
                )
                execution_skipped = True

        update_heartbeat()
        control_final = load_bot_control()

        return self.completed_result(
            started,
            stages,
            control_before,
            control_final,
            intents_skipped=not control_after_scan[
                "new_entries_enabled"
            ],
            execution_skipped=execution_skipped,
        )

    def failed_result(
        self,
        started,
        stages,
        control_before,
    ):
        try:
            update_heartbeat()
        except Exception:
            pass

        return {
            "status": "failed",
            "mode": "run",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc),
            "bot_control_before": control_before,
            "stages": stages,
            "summary": [
                stage_summary(stage)
                for stage in stages
                if "returncode" in stage
            ],
            "failed_stage": (
                stages[-1]["stage"]
                if stages
                else None
            ),
            "company_news_refresh_ingestion_productionized": True,
        }

    def completed_result(
        self,
        started,
        stages,
        control_before,
        control_final,
        intents_skipped,
        execution_skipped,
    ):
        return {
            "status": "completed",
            "mode": "run",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc),
            "bot_control_before": control_before,
            "bot_control_final": control_final,
            "stages": stages,
            "summary": [
                stage_summary(stage)
                for stage in stages
                if "returncode" in stage
            ],
            "intents_skipped": intents_skipped,
            "execution_skipped": execution_skipped,
            "company_news_refresh_ingestion_productionized": True,
            "company_news_ai_on_persisted_clusters": True,
            "market_news_provider_failover": True,
        }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Top-level DELTAX autonomous agent cycle."
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "Read-only health check across all current production stages."
        ),
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help=(
            "Run one DELTAX autonomous cycle. "
            "Execution still obeys bot_control."
        ),
    )

    parser.add_argument(
        "--market-lookback-hours",
        type=int,
        default=DEFAULT_MARKET_LOOKBACK_HOURS,
    )
    parser.add_argument(
        "--market-cluster-lookback-hours",
        type=int,
        default=DEFAULT_MARKET_CLUSTER_LOOKBACK_HOURS,
    )
    parser.add_argument(
        "--market-ai-since-hours",
        type=int,
        default=DEFAULT_MARKET_AI_SINCE_HOURS,
    )
    parser.add_argument(
        "--company-news-lookback-hours",
        type=int,
        default=DEFAULT_COMPANY_NEWS_LOOKBACK_HOURS,
    )
    parser.add_argument(
        "--company-ai-since-hours",
        type=int,
        default=DEFAULT_COMPANY_AI_SINCE_HOURS,
    )
    parser.add_argument(
        "--market-ai-limit",
        type=int,
        default=DEFAULT_MARKET_AI_LIMIT,
    )
    parser.add_argument(
        "--company-ai-limit",
        type=int,
        default=DEFAULT_COMPANY_AI_LIMIT,
    )
    parser.add_argument(
        "--execution-limit",
        type=int,
        default=DEFAULT_EXECUTION_LIMIT,
    )

    args = parser.parse_args()

    positive_values = {
        "market-lookback-hours": args.market_lookback_hours,
        "market-cluster-lookback-hours":
            args.market_cluster_lookback_hours,
        "market-ai-since-hours": args.market_ai_since_hours,
        "company-news-lookback-hours": args.company_news_lookback_hours,
        "company-ai-since-hours": args.company_ai_since_hours,
        "market-ai-limit": args.market_ai_limit,
        "company-ai-limit": args.company_ai_limit,
        "execution-limit": args.execution_limit,
    }

    for name, value in positive_values.items():
        if value <= 0:
            parser.error(f"--{name} must be greater than zero")

    return args


def main():
    args = parse_args()

    cycle = AgentCycle(
        market_lookback_hours=args.market_lookback_hours,
        market_cluster_lookback_hours=
            args.market_cluster_lookback_hours,
        market_ai_since_hours=args.market_ai_since_hours,
        company_news_lookback_hours=args.company_news_lookback_hours,
        company_ai_since_hours=args.company_ai_since_hours,
        market_ai_limit=args.market_ai_limit,
        company_ai_limit=args.company_ai_limit,
        execution_limit=args.execution_limit,
    )

    result = (
        cycle.health_check()
        if args.check
        else cycle.run()
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=json_default,
        )
    )

    if result["status"] == "failed":
        print("AGENT CYCLE: FAILED", file=sys.stderr)
        sys.exit(1)

    print("AGENT CYCLE: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
