# File: deltax/direction_router.py
# Purpose: Routes production Core, Active, and Intraday candidates into deterministic long, short, awaiting-confirmation, or rejected decisions.

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
EXPECTED_CONFIG_VERSION = "deltax_v2_strategy_v2"


@dataclass(frozen=True)
class NewsAnalysis:
    cluster_key: str
    published_at: datetime
    processed: bool
    direction: Optional[str] = None
    confidence: Optional[float] = None
    meaningful_company_specific_catalyst: Optional[bool] = None
    sufficient_news: Optional[bool] = None
    event_cluster_id: Optional[UUID] = None
    ai_analysis_id: Optional[UUID] = None


@dataclass
class RouteCandidate:
    symbol: str
    strategy: str
    deviation_side: str
    signal_at: datetime
    signal_price: Decimal | float | int
    now: datetime
    news_analyses: list[NewsAnalysis] = field(default_factory=list)
    confirmation_due_at: Optional[datetime] = None
    confirmation_checked_at: Optional[datetime] = None
    confirmation_price: Optional[Decimal | float | int] = None
    technical_direction: Optional[str] = None
    market_open: Optional[bool] = None
    session_open: Optional[datetime] = None
    previous_session_close: Optional[datetime] = None
    shortable: Optional[bool] = None
    easy_to_borrow: Optional[bool] = None


@dataclass(frozen=True)
class RouteDecision:
    symbol: str
    strategy: str
    direction: str
    status: str
    mode: str
    confirmation_required: bool
    confirmation_passed: Optional[bool]
    rejection_reasons: list[str]
    active_news_cluster_keys: list[str]
    primary_ai_analysis_id: Optional[UUID]
    decided_at: datetime

    @property
    def approved(self):
        return self.status == "approved"

    def technical_state(self, candidate):
        return {
            "deviation_side": candidate.deviation_side,
            "technical_direction": candidate.technical_direction,
            "decision_mode": self.mode,
            "confirmation_required": self.confirmation_required,
            "router_status": self.status,
            "active_news_cluster_keys": self.active_news_cluster_keys,
        }

    def risk_state(self, candidate):
        state = {
            "shortability_checked": self.direction == "short",
            "rejection_reasons": self.rejection_reasons,
        }

        if self.direction == "short":
            state["alpaca_shortable"] = candidate.shortable
            state["alpaca_easy_to_borrow"] = candidate.easy_to_borrow

        return state


def require_aware_datetime(value, field_name):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def validate_candidate(candidate):
    if not candidate.symbol or candidate.symbol != candidate.symbol.upper():
        raise ValueError("symbol must be a non-empty uppercase value")

    if candidate.strategy not in {"core", "active", "intraday"}:
        raise ValueError(
            f"Unsupported strategy: {candidate.strategy}"
        )

    if candidate.deviation_side not in {"downside", "upside"}:
        raise ValueError(
            f"Unsupported deviation side: {candidate.deviation_side}"
        )

    require_aware_datetime(candidate.signal_at, "signal_at")
    require_aware_datetime(candidate.now, "now")

    if Decimal(str(candidate.signal_price)) <= 0:
        raise ValueError("signal_price must be greater than zero")

    if candidate.confirmation_due_at is not None:
        require_aware_datetime(
            candidate.confirmation_due_at,
            "confirmation_due_at",
        )

    if candidate.confirmation_checked_at is not None:
        require_aware_datetime(
            candidate.confirmation_checked_at,
            "confirmation_checked_at",
        )

    for analysis in candidate.news_analyses:
        require_aware_datetime(
            analysis.published_at,
            "news published_at",
        )


class DirectionRouter:
    def __init__(self, config):
        self.config = config
        self.confidence_threshold = float(
            config["news_rules"][
                "directional_confidence_threshold"
            ]
        )
        self.intraday_news_minutes = int(
            config["news_rules"]["intraday_active_window"][
                "regular_session_lookback_minutes"
            ]
        )
        self._validate_config()

    @classmethod
    def from_database(cls, database_url=DATABASE_URL):
        with psycopg.connect(
            database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        version,
                        config
                    FROM strategy_configs
                    WHERE is_active = true
                    ORDER BY activated_at DESC NULLS LAST,
                             created_at DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()

        if row is None:
            raise RuntimeError("No active strategy configuration found")

        if row["version"] != EXPECTED_CONFIG_VERSION:
            raise RuntimeError(
                "Direction router requires active config "
                f"{EXPECTED_CONFIG_VERSION}, found {row['version']}"
            )

        return cls(row["config"])

    def _validate_config(self):
        router = self.config.get("direction_router", {})
        core_active = router.get("core_active", {})
        intraday = router.get("intraday", {})

        if core_active.get("confirmation_minutes") != 10:
            raise ValueError(
                "Core/Active confirmation must be 10 minutes"
            )

        if intraday.get("confirmation_required") is not False:
            raise ValueError(
                "Intraday price confirmation must be disabled"
            )

        if self.confidence_threshold != 0.65:
            raise ValueError(
                "Directional confidence threshold must be 0.65"
            )

    def is_material(self, analysis):
        return (
            analysis.processed
            and analysis.direction in {"bullish", "bearish"}
            and analysis.confidence is not None
            and float(analysis.confidence)
            >= self.confidence_threshold
            and analysis.meaningful_company_specific_catalyst is True
            and analysis.sufficient_news is True
        )

    def select_active_intraday_news(self, candidate):
        if (
            candidate.session_open is None
            or candidate.previous_session_close is None
        ):
            raise ValueError(
                "Intraday routing requires session_open and "
                "previous_session_close"
            )

        require_aware_datetime(
            candidate.session_open,
            "session_open",
        )
        require_aware_datetime(
            candidate.previous_session_close,
            "previous_session_close",
        )

        regular_cutoff = candidate.now - timedelta(
            minutes=self.intraday_news_minutes
        )
        active = []

        for analysis in candidate.news_analyses:
            published_at = analysis.published_at

            if published_at > candidate.now:
                continue

            premarket = (
                candidate.previous_session_close
                <= published_at
                < candidate.session_open
            )
            recent_regular = (
                candidate.session_open
                <= published_at
                <= candidate.now
                and published_at >= regular_cutoff
            )

            if premarket or recent_regular:
                active.append(analysis)

        return sorted(active, key=lambda item: item.published_at)

    def summarize_news(self, analyses):
        unprocessed = [
            analysis
            for analysis in analyses
            if not analysis.processed
        ]
        material_directions = {
            analysis.direction
            for analysis in analyses
            if self.is_material(analysis)
        }
        material = [
            analysis
            for analysis in analyses
            if self.is_material(analysis)
        ]

        return unprocessed, material_directions, material

    def primary_ai_analysis_id(self, analyses, direction):
        matching = [
            analysis
            for analysis in analyses
            if analysis.processed
            and analysis.direction == direction
            and analysis.ai_analysis_id is not None
        ]

        if not matching:
            return None

        matching.sort(
            key=lambda item: (
                float(item.confidence or 0),
                item.published_at,
            ),
            reverse=True,
        )
        return matching[0].ai_analysis_id

    def primary_relevant_ai_analysis_id(self, analyses):
        candidates = [
            analysis
            for analysis in analyses
            if analysis.processed
            and analysis.ai_analysis_id is not None
        ]

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                self.is_material(item),
                float(item.confidence or 0),
                item.published_at,
            ),
            reverse=True,
        )
        return candidates[0].ai_analysis_id

    def reject(
        self,
        candidate,
        direction,
        mode,
        reasons,
        active_news,
        confirmation_required,
        confirmation_passed=None,
    ):
        return RouteDecision(
            symbol=candidate.symbol,
            strategy=candidate.strategy,
            direction=direction,
            status="rejected",
            mode=mode,
            confirmation_required=confirmation_required,
            confirmation_passed=confirmation_passed,
            rejection_reasons=list(dict.fromkeys(reasons)),
            active_news_cluster_keys=[
                analysis.cluster_key
                for analysis in active_news
            ],
            primary_ai_analysis_id=(
                self.primary_relevant_ai_analysis_id(active_news)
            ),
            decided_at=candidate.now,
        )

    def apply_shortability_gate(
        self,
        candidate,
        direction,
        mode,
        active_news,
        confirmation_required,
    ):
        if direction != "short":
            return None

        reasons = []

        if candidate.shortable is not True:
            reasons.append("symbol_not_shortable")

        if candidate.easy_to_borrow is not True:
            reasons.append("symbol_not_easy_to_borrow")

        if not reasons:
            return None

        return self.reject(
            candidate=candidate,
            direction=direction,
            mode=mode,
            reasons=reasons,
            active_news=active_news,
            confirmation_required=confirmation_required,
        )

    def route(self, candidate):
        validate_candidate(candidate)

        if candidate.strategy in {"core", "active"}:
            return self.route_core_active(candidate)

        return self.route_intraday(candidate)

    def route_core_active(self, candidate):
        active_news = sorted(
            candidate.news_analyses,
            key=lambda item: item.published_at,
        )
        unprocessed, material_directions, _ = self.summarize_news(
            active_news
        )

        if candidate.deviation_side == "downside":
            if "bearish" in material_directions:
                direction = "short"
                mode = "news_momentum"
            else:
                direction = "long"
                mode = "mean_reversion"
        else:
            if "bullish" in material_directions:
                direction = "long"
                mode = "news_momentum"
            else:
                direction = "short"
                mode = "mean_reversion"

        if unprocessed:
            return self.reject(
                candidate,
                direction,
                mode,
                ["fresh_news_not_analyzed"],
                active_news,
                True,
            )

        if material_directions == {"bullish", "bearish"}:
            technical_direction = (
                "long"
                if candidate.deviation_side == "downside"
                else "short"
            )
            return self.reject(
                candidate,
                technical_direction,
                "mean_reversion",
                ["conflicting_material_news"],
                active_news,
                True,
            )

        short_rejection = self.apply_shortability_gate(
            candidate,
            direction,
            mode,
            active_news,
            True,
        )

        if short_rejection is not None:
            return short_rejection

        if candidate.confirmation_due_at is None:
            return self.reject(
                candidate,
                direction,
                mode,
                ["missing_confirmation_due_at"],
                active_news,
                True,
            )

        if (
            candidate.now < candidate.confirmation_due_at
            and candidate.confirmation_price is None
        ):
            return RouteDecision(
                symbol=candidate.symbol,
                strategy=candidate.strategy,
                direction=direction,
                status="awaiting_confirmation",
                mode=mode,
                confirmation_required=True,
                confirmation_passed=None,
                rejection_reasons=[],
                active_news_cluster_keys=[
                    analysis.cluster_key
                    for analysis in active_news
                ],
                primary_ai_analysis_id=(
                    self.primary_ai_analysis_id(
                        active_news,
                        "bearish"
                        if direction == "short"
                        else "bullish",
                    )
                ),
                decided_at=candidate.now,
            )

        if candidate.confirmation_price is None:
            return self.reject(
                candidate,
                direction,
                mode,
                ["confirmation_price_missing_after_due_at"],
                active_news,
                True,
            )

        if candidate.confirmation_checked_at is None:
            return self.reject(
                candidate,
                direction,
                mode,
                ["confirmation_checked_at_missing"],
                active_news,
                True,
            )

        signal_price = Decimal(str(candidate.signal_price))
        confirmation_price = Decimal(
            str(candidate.confirmation_price)
        )
        confirmation_passed = (
            confirmation_price > signal_price
            if direction == "long"
            else confirmation_price < signal_price
        )

        if not confirmation_passed:
            reason = (
                "long_price_confirmation_failed"
                if direction == "long"
                else "short_price_confirmation_failed"
            )
            return self.reject(
                candidate,
                direction,
                mode,
                [reason],
                active_news,
                True,
                False,
            )

        return RouteDecision(
            symbol=candidate.symbol,
            strategy=candidate.strategy,
            direction=direction,
            status="approved",
            mode=mode,
            confirmation_required=True,
            confirmation_passed=True,
            rejection_reasons=[],
            active_news_cluster_keys=[
                analysis.cluster_key
                for analysis in active_news
            ],
            primary_ai_analysis_id=self.primary_ai_analysis_id(
                active_news,
                "bearish" if direction == "short" else "bullish",
            ),
            decided_at=candidate.now,
        )

    def route_intraday(self, candidate):
        direction = (
            "long"
            if candidate.deviation_side == "downside"
            else "short"
        )
        mode = "intraday_mean_reversion"

        if (
            candidate.technical_direction is not None
            and candidate.technical_direction != direction
        ):
            return self.reject(
                candidate,
                direction,
                mode,
                ["technical_direction_does_not_match_deviation"],
                [],
                False,
            )

        if candidate.market_open is not True:
            return self.reject(
                candidate,
                direction,
                mode,
                ["regular_market_not_open"],
                [],
                False,
            )

        active_news = self.select_active_intraday_news(candidate)
        unprocessed, material_directions, _ = self.summarize_news(
            active_news
        )

        if unprocessed:
            return self.reject(
                candidate,
                direction,
                mode,
                ["fresh_news_not_analyzed"],
                active_news,
                False,
            )

        if material_directions == {"bullish", "bearish"}:
            return self.reject(
                candidate,
                direction,
                mode,
                ["conflicting_material_news"],
                active_news,
                False,
            )

        if direction == "long" and "bearish" in material_directions:
            return self.reject(
                candidate,
                direction,
                mode,
                ["active_bearish_news_veto"],
                active_news,
                False,
            )

        if direction == "short" and "bullish" in material_directions:
            return self.reject(
                candidate,
                direction,
                mode,
                ["active_bullish_news_veto"],
                active_news,
                False,
            )

        short_rejection = self.apply_shortability_gate(
            candidate,
            direction,
            mode,
            active_news,
            False,
        )

        if short_rejection is not None:
            return short_rejection

        return RouteDecision(
            symbol=candidate.symbol,
            strategy=candidate.strategy,
            direction=direction,
            status="approved",
            mode=mode,
            confirmation_required=False,
            confirmation_passed=None,
            rejection_reasons=[],
            active_news_cluster_keys=[
                analysis.cluster_key
                for analysis in active_news
            ],
            primary_ai_analysis_id=self.primary_ai_analysis_id(
                active_news,
                "bearish" if direction == "short" else "bullish",
            ),
            decided_at=candidate.now,
        )

    def health_check(self):
        return {
            "config_version": EXPECTED_CONFIG_VERSION,
            "confidence_threshold": self.confidence_threshold,
            "core_confirmation_minutes": self.config[
                "direction_router"
            ]["core_active"]["confirmation_minutes"],
            "active_confirmation_minutes": self.config[
                "direction_router"
            ]["core_active"]["confirmation_minutes"],
            "intraday_confirmation_required": self.config[
                "direction_router"
            ]["intraday"]["confirmation_required"],
            "intraday_news_minutes": self.intraday_news_minutes,
            "writes_performed": False,
        }


def parse_args():
    parser = argparse.ArgumentParser(
        description="DELTAX production direction router."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Load and validate the active routing configuration.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.check:
        print(
            "This production module is imported by the scan-cycle "
            "orchestrator. Use --check for a read-only health check."
        )
        return

    router = DirectionRouter.from_database()
    print(
        json.dumps(
            router.health_check(),
            indent=2,
            ensure_ascii=False,
        )
    )
    print("DIRECTION ROUTER HEALTH CHECK: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
