# File: deltax/decision_persistence.py
# Purpose:
# Persists production scan runs and routed trade theses without creating
# trade intents or broker orders.
#
# Important:
# New theses may only be created while their scan_run is running.
# Existing theses may be updated after the original scan_run has completed.
# This is required for Core/Active 10-minute confirmation across 5-minute
# scan cycles.

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
SCAN_INTERVAL_MINUTES = 5

ALLOWED_SCAN_STATUSES = {
    "running",
    "completed",
    "partial",
    "failed",
    "skipped",
}

ALLOWED_STRATEGIES = {
    "core",
    "active",
    "intraday",
}

ALLOWED_DIRECTIONS = {
    "long",
    "short",
}

ALLOWED_THESIS_STATUSES = {
    "detected",
    "awaiting_ai",
    "awaiting_confirmation",
    "approved",
    "rejected",
    "expired",
    "intents_created",
}

ALLOWED_STATUS_TRANSITIONS = {
    "detected": {
        "detected",
        "awaiting_ai",
        "awaiting_confirmation",
        "approved",
        "rejected",
        "expired",
    },
    "awaiting_ai": {
        "awaiting_ai",
        "awaiting_confirmation",
        "approved",
        "rejected",
        "expired",
    },
    "awaiting_confirmation": {
        "awaiting_confirmation",
        "approved",
        "rejected",
        "expired",
    },
    "approved": {
        "approved",
        "intents_created",
        "expired",
    },
    "rejected": {"rejected"},
    "expired": {"expired"},
    "intents_created": {"intents_created"},
}


@dataclass(frozen=True)
class ScanRunStart:
    id: UUID
    strategy_config_id: UUID
    scanner_name: str
    scheduled_for: datetime
    created: bool
    status: str


@dataclass
class TradeThesisInput:
    scan_run_id: UUID
    strategy_config_id: UUID
    symbol: str
    strategy: str
    direction: str
    status: str
    signal_at: datetime
    signal_price: Decimal | float | int
    expires_at: datetime

    ai_analysis_id: Optional[UUID] = None

    reference_vwap: Optional[Decimal | float | int] = None
    deviation_pct: Optional[Decimal | float | int] = None
    atr_14: Optional[Decimal | float | int] = None
    atr_pct: Optional[Decimal | float | int] = None
    weak_indices_count: Optional[int] = None

    technical_state: dict[str, Any] = field(default_factory=dict)
    market_state: dict[str, Any] = field(default_factory=dict)
    sector_state: dict[str, Any] = field(default_factory=dict)
    risk_state: dict[str, Any] = field(default_factory=dict)

    confirmation_due_at: Optional[datetime] = None
    confirmation_checked_at: Optional[datetime] = None
    confirmation_price: Optional[Decimal | float | int] = None
    confirmation_passed: Optional[bool] = None

    rejection_reasons: list[str] = field(default_factory=list)


def require_aware_datetime(value, field_name):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{field_name} must be timezone-aware"
        )


def normalize_scheduled_for(value=None):
    current = value or datetime.now(timezone.utc)

    require_aware_datetime(
        current,
        "scheduled_for",
    )

    current = current.astimezone(timezone.utc)

    floored_minute = (
        current.minute // SCAN_INTERVAL_MINUTES
    ) * SCAN_INTERVAL_MINUTES

    return current.replace(
        minute=floored_minute,
        second=0,
        microsecond=0,
    )


def validate_thesis(thesis):
    if thesis.strategy not in ALLOWED_STRATEGIES:
        raise ValueError(
            f"Unsupported strategy: {thesis.strategy}"
        )

    if thesis.direction not in ALLOWED_DIRECTIONS:
        raise ValueError(
            "Trade thesis direction must be long or short. "
            "Use status='rejected' for a rejected candidate."
        )

    if thesis.status not in ALLOWED_THESIS_STATUSES:
        raise ValueError(
            f"Unsupported trade thesis status: {thesis.status}"
        )

    if (
        not thesis.symbol
        or thesis.symbol != thesis.symbol.upper()
    ):
        raise ValueError(
            "symbol must be a non-empty uppercase value"
        )

    require_aware_datetime(
        thesis.signal_at,
        "signal_at",
    )

    require_aware_datetime(
        thesis.expires_at,
        "expires_at",
    )

    if thesis.confirmation_due_at is not None:
        require_aware_datetime(
            thesis.confirmation_due_at,
            "confirmation_due_at",
        )

    if thesis.confirmation_checked_at is not None:
        require_aware_datetime(
            thesis.confirmation_checked_at,
            "confirmation_checked_at",
        )

    if Decimal(str(thesis.signal_price)) <= 0:
        raise ValueError(
            "signal_price must be greater than zero"
        )

    if thesis.expires_at <= thesis.signal_at:
        raise ValueError(
            "expires_at must be after signal_at"
        )

    if thesis.weak_indices_count is not None:
        if thesis.weak_indices_count not in {
            0,
            1,
            2,
            3,
        }:
            raise ValueError(
                "weak_indices_count must be between 0 and 3"
            )

    if (
        thesis.status == "rejected"
        and not thesis.rejection_reasons
    ):
        raise ValueError(
            "A rejected thesis requires at least "
            "one rejection reason"
        )

    if thesis.strategy == "intraday":
        confirmation_values = {
            "confirmation_due_at":
                thesis.confirmation_due_at,
            "confirmation_checked_at":
                thesis.confirmation_checked_at,
            "confirmation_price":
                thesis.confirmation_price,
            "confirmation_passed":
                thesis.confirmation_passed,
        }

        populated = [
            name
            for name, value
            in confirmation_values.items()
            if value is not None
        ]

        if populated:
            raise ValueError(
                "Intraday theses must not contain "
                "10-minute confirmation fields: "
                + ", ".join(populated)
            )

    if thesis.status == "awaiting_confirmation":
        if thesis.strategy not in {
            "core",
            "active",
        }:
            raise ValueError(
                "Only Core and Active may await confirmation"
            )

        if thesis.confirmation_due_at is None:
            raise ValueError(
                "awaiting_confirmation requires "
                "confirmation_due_at"
            )


def validate_status_transition(
    current_status,
    next_status,
):
    allowed = ALLOWED_STATUS_TRANSITIONS.get(
        current_status,
        set(),
    )

    if next_status not in allowed:
        raise ValueError(
            "Invalid trade thesis status transition: "
            f"{current_status} -> {next_status}"
        )


class DecisionPersistence:
    def __init__(
        self,
        database_url=DATABASE_URL,
    ):
        self.database_url = database_url

    def get_active_strategy_config(
        self,
        cursor,
    ):
        cursor.execute(
            """
            SELECT
                id,
                version,
                name,
                config
            FROM strategy_configs
            WHERE is_active = true
            ORDER BY
                activated_at DESC NULLS LAST,
                created_at DESC
            LIMIT 1
            """
        )

        row = cursor.fetchone()

        if row is None:
            raise RuntimeError(
                "No active strategy configuration found"
            )

        return row

    def start_scan_run(
        self,
        scanner_name,
        scheduled_for=None,
        market_open=None,
        symbols_requested=0,
        metadata=None,
    ):
        if not scanner_name.strip():
            raise ValueError(
                "scanner_name must not be empty"
            )

        if symbols_requested < 0:
            raise ValueError(
                "symbols_requested must not be negative"
            )

        scheduled_for = normalize_scheduled_for(
            scheduled_for
        )

        metadata = metadata or {}

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:

            with connection.cursor() as cursor:
                strategy_config = (
                    self.get_active_strategy_config(
                        cursor
                    )
                )

                cursor.execute(
                    """
                    INSERT INTO scan_runs (
                        strategy_config_id,
                        scanner_name,
                        scheduled_for,
                        status,
                        market_open,
                        symbols_requested,
                        metadata
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        'running',
                        %s,
                        %s,
                        %s
                    )
                    ON CONFLICT (
                        scanner_name,
                        scheduled_for
                    )
                    DO NOTHING
                    RETURNING
                        id,
                        strategy_config_id,
                        scanner_name,
                        scheduled_for,
                        status
                    """,
                    (
                        strategy_config["id"],
                        scanner_name,
                        scheduled_for,
                        market_open,
                        symbols_requested,
                        Jsonb(metadata),
                    ),
                )

                inserted = cursor.fetchone()

                if inserted is not None:
                    connection.commit()

                    return ScanRunStart(
                        id=inserted["id"],
                        strategy_config_id=inserted[
                            "strategy_config_id"
                        ],
                        scanner_name=inserted[
                            "scanner_name"
                        ],
                        scheduled_for=inserted[
                            "scheduled_for"
                        ],
                        created=True,
                        status=inserted["status"],
                    )

                cursor.execute(
                    """
                    SELECT
                        id,
                        strategy_config_id,
                        scanner_name,
                        scheduled_for,
                        status
                    FROM scan_runs
                    WHERE scanner_name = %s
                      AND scheduled_for = %s
                    """,
                    (
                        scanner_name,
                        scheduled_for,
                    ),
                )

                existing = cursor.fetchone()

                connection.commit()

                if existing is None:
                    raise RuntimeError(
                        "Scan run conflict occurred but "
                        "the existing record could not "
                        "be loaded"
                    )

                return ScanRunStart(
                    id=existing["id"],
                    strategy_config_id=existing[
                        "strategy_config_id"
                    ],
                    scanner_name=existing[
                        "scanner_name"
                    ],
                    scheduled_for=existing[
                        "scheduled_for"
                    ],
                    created=False,
                    status=existing["status"],
                )

    def finish_scan_run(
        self,
        scan_run_id,
        status,
        symbols_processed,
        signals_found,
        error_message=None,
        metadata=None,
    ):
        if status not in (
            ALLOWED_SCAN_STATUSES - {"running"}
        ):
            raise ValueError(
                f"Invalid terminal scan status: {status}"
            )

        if (
            symbols_processed < 0
            or signals_found < 0
        ):
            raise ValueError(
                "Scan counters must not be negative"
            )

        metadata = metadata or {}

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE scan_runs
                    SET
                        finished_at = now(),
                        status = %s,
                        symbols_processed = %s,
                        signals_found = %s,
                        error_message = %s,
                        metadata = metadata || %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (
                        status,
                        symbols_processed,
                        signals_found,
                        error_message,
                        Jsonb(metadata),
                        scan_run_id,
                    ),
                )

                updated = cursor.fetchone()

                if updated is None:
                    raise RuntimeError(
                        "Scan run not found: "
                        f"{scan_run_id}"
                    )

                connection.commit()

                return updated

    def load_due_confirmations(
        self,
        now=None,
        limit=100,
    ):
        """
        Load Core/Active theses whose 10-minute
        confirmation time has arrived.

        The thesis may belong to an already-completed
        scan run. That is intentional.
        """

        current_time = (
            now
            or datetime.now(timezone.utc)
        )

        require_aware_datetime(
            current_time,
            "now",
        )

        if limit <= 0:
            raise ValueError(
                "limit must be greater than zero"
            )

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        theses.*,
                        runs.status AS scan_run_status
                    FROM trade_theses theses
                    JOIN scan_runs runs
                      ON runs.id = theses.scan_run_id
                    WHERE theses.status =
                            'awaiting_confirmation'
                      AND theses.strategy IN (
                            'core',
                            'active'
                      )
                      AND theses.confirmation_due_at
                            <= %s
                      AND theses.expires_at > %s
                    ORDER BY
                        theses.confirmation_due_at,
                        theses.created_at
                    LIMIT %s
                    """,
                    (
                        current_time,
                        current_time,
                        limit,
                    ),
                )

                return cursor.fetchall()

    def load_expired_open_theses(
        self,
        now=None,
        limit=100,
    ):
        """
        Returns unfinished theses whose expiry
        time has passed.
        """

        current_time = (
            now
            or datetime.now(timezone.utc)
        )

        require_aware_datetime(
            current_time,
            "now",
        )

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT *
                    FROM trade_theses
                    WHERE status IN (
                        'detected',
                        'awaiting_ai',
                        'awaiting_confirmation',
                        'approved'
                    )
                      AND expires_at <= %s
                    ORDER BY expires_at
                    LIMIT %s
                    """,
                    (
                        current_time,
                        limit,
                    ),
                )

                return cursor.fetchall()

    def persist_trade_thesis(
        self,
        thesis,
    ):
        """
        Create or update a trade thesis.

        Safety rule:
        - a NEW thesis may only be created while
          its originating scan_run is running;
        - an EXISTING thesis may continue to be
          updated after that scan has completed.

        This allows Core/Active 10-minute price
        confirmation to span multiple 5-minute
        scan cycles.
        """

        validate_thesis(thesis)

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        strategy_config_id,
                        status
                    FROM scan_runs
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (
                        thesis.scan_run_id,
                    ),
                )

                scan_run = cursor.fetchone()

                if scan_run is None:
                    raise RuntimeError(
                        "Scan run not found: "
                        f"{thesis.scan_run_id}"
                    )

                if (
                    scan_run["strategy_config_id"]
                    != thesis.strategy_config_id
                ):
                    raise ValueError(
                        "Trade thesis strategy_config_id "
                        "does not match the scan run"
                    )

                cursor.execute(
                    """
                    SELECT
                        id,
                        status
                    FROM trade_theses
                    WHERE scan_run_id = %s
                      AND symbol = %s
                      AND strategy = %s
                      AND direction = %s
                    FOR UPDATE
                    """,
                    (
                        thesis.scan_run_id,
                        thesis.symbol,
                        thesis.strategy,
                        thesis.direction,
                    ),
                )

                existing = cursor.fetchone()

                # Important:
                # Only NEW theses require the original
                # scan run still to be running.
                #
                # Existing awaiting-confirmation theses
                # must remain updateable after that
                # scan run has completed.
                if (
                    existing is None
                    and scan_run["status"] != "running"
                ):
                    raise RuntimeError(
                        "New trade theses may only be "
                        "created while the scan run is "
                        "running, found "
                        f"{scan_run['status']}"
                    )

                values = (
                    thesis.ai_analysis_id,
                    thesis.status,
                    thesis.signal_at,
                    thesis.signal_price,
                    thesis.reference_vwap,
                    thesis.deviation_pct,
                    thesis.atr_14,
                    thesis.atr_pct,
                    thesis.weak_indices_count,
                    Jsonb(
                        thesis.technical_state
                    ),
                    Jsonb(
                        thesis.market_state
                    ),
                    Jsonb(
                        thesis.sector_state
                    ),
                    Jsonb(
                        thesis.risk_state
                    ),
                    thesis.confirmation_due_at,
                    thesis.confirmation_checked_at,
                    thesis.confirmation_price,
                    thesis.confirmation_passed,
                    thesis.expires_at,
                    thesis.rejection_reasons,
                )

                if existing is None:
                    cursor.execute(
                        """
                        INSERT INTO trade_theses (
                            scan_run_id,
                            strategy_config_id,
                            symbol,
                            strategy,
                            direction,
                            ai_analysis_id,
                            status,
                            signal_at,
                            signal_price,
                            reference_vwap,
                            deviation_pct,
                            atr_14,
                            atr_pct,
                            weak_indices_count,
                            technical_state,
                            market_state,
                            sector_state,
                            risk_state,
                            confirmation_due_at,
                            confirmation_checked_at,
                            confirmation_price,
                            confirmation_passed,
                            expires_at,
                            rejection_reasons
                        )
                        VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s
                        )
                        RETURNING *
                        """,
                        (
                            thesis.scan_run_id,
                            thesis.strategy_config_id,
                            thesis.symbol,
                            thesis.strategy,
                            thesis.direction,
                            *values,
                        ),
                    )

                else:
                    validate_status_transition(
                        existing["status"],
                        thesis.status,
                    )

                    cursor.execute(
                        """
                        UPDATE trade_theses
                        SET
                            ai_analysis_id = %s,
                            status = %s,
                            signal_at = %s,
                            signal_price = %s,
                            reference_vwap = %s,
                            deviation_pct = %s,
                            atr_14 = %s,
                            atr_pct = %s,
                            weak_indices_count = %s,
                            technical_state = %s,
                            market_state = %s,
                            sector_state = %s,
                            risk_state = %s,
                            confirmation_due_at = %s,
                            confirmation_checked_at = %s,
                            confirmation_price = %s,
                            confirmation_passed = %s,
                            expires_at = %s,
                            rejection_reasons = %s,
                            updated_at = now()
                        WHERE id = %s
                        RETURNING *
                        """,
                        (
                            *values,
                            existing["id"],
                        ),
                    )

                saved = cursor.fetchone()

                connection.commit()

                return saved

    def health_check(self):
        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:

            with connection.cursor() as cursor:
                strategy_config = (
                    self.get_active_strategy_config(
                        cursor
                    )
                )

                cursor.execute(
                    """
                    SELECT
                        (
                            SELECT COUNT(*)
                            FROM scan_runs
                        ) AS scan_runs,

                        (
                            SELECT COUNT(*)
                            FROM trade_theses
                        ) AS trade_theses,

                        (
                            SELECT COUNT(*)
                            FROM trade_intents
                        ) AS trade_intents,

                        (
                            SELECT COUNT(*)
                            FROM broker_orders
                        ) AS broker_orders,

                        (
                            SELECT COUNT(*)
                            FROM trade_theses
                            WHERE status =
                                'awaiting_confirmation'
                        ) AS awaiting_confirmation,

                        (
                            SELECT COUNT(*)
                            FROM trade_theses
                            WHERE status =
                                'awaiting_confirmation'
                              AND confirmation_due_at
                                    <= now()
                              AND expires_at > now()
                        ) AS due_confirmations
                    """
                )

                counts = cursor.fetchone()

        return {
            "active_strategy_config_id":
                str(strategy_config["id"]),
            "active_strategy_version":
                strategy_config["version"],
            "active_strategy_name":
                strategy_config["name"],
            "counts":
                dict(counts),
            "cross_scan_confirmation_updates":
                True,
            "writes_performed":
                False,
        }


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "DELTAX decision-persistence "
            "production module."
        )
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Run a read-only database and "
            "configuration health check."
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if not args.check:
        print(
            "This production module is imported "
            "by the scan-cycle orchestrator. "
            "Use --check for a read-only "
            "health check."
        )
        return

    repository = DecisionPersistence()

    result = repository.health_check()

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )

    print(
        "DECISION PERSISTENCE HEALTH CHECK: OK"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print(
            f"ERROR: {error}",
            file=sys.stderr,
        )
        sys.exit(1)