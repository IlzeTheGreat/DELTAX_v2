# File: deltax/news_context.py
# Purpose: Loads fresh clustered news and current AI classifications for the production direction router without using obsolete stored gate decisions.

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

try:
    from deltax.direction_router import NewsAnalysis
except ModuleNotFoundError:
    from direction_router import NewsAnalysis


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
PROMPT_VERSION = "deltax_news_cluster_v2"


def require_aware_datetime(value, field_name):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class NewsContextRepository:
    def __init__(self, database_url=DATABASE_URL):
        self.database_url = database_url

    def load_for_symbols(
        self,
        symbols,
        published_after,
        now=None,
    ):
        normalized_symbols = sorted(
            {
                symbol.upper()
                for symbol in symbols
                if symbol and symbol.strip()
            }
        )

        if not normalized_symbols:
            return {}

        require_aware_datetime(published_after, "published_after")
        current_time = now or datetime.now(timezone.utc)
        require_aware_datetime(current_time, "now")

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH cluster_freshness AS (
                        SELECT
                            clusters.id AS event_cluster_id,
                            MAX(events.ingested_at) AS latest_ingested_at
                        FROM event_clusters clusters
                        JOIN event_cluster_members members
                            ON members.event_cluster_id = clusters.id
                        JOIN source_events events
                            ON events.id = members.source_event_id
                        WHERE clusters.primary_symbol = ANY(%s)
                          AND clusters.event_type = 'news'
                          AND clusters.last_published_at >= %s
                          AND clusters.first_published_at <= %s
                        GROUP BY clusters.id
                    ),
                    latest_completed_analysis AS (
                        SELECT DISTINCT ON (
                            analyses.event_cluster_id,
                            analyses.symbol
                        )
                            analyses.id,
                            analyses.event_cluster_id,
                            analyses.symbol,
                            analyses.direction,
                            analyses.confidence,
                            analyses.raw_response,
                            analyses.completed_at
                        FROM ai_analyses analyses
                        WHERE analyses.status = 'completed'
                          AND analyses.event_cluster_id IS NOT NULL
                          AND analyses.prompt_version = %s
                          AND analyses.symbol = ANY(%s)
                        ORDER BY
                            analyses.event_cluster_id,
                            analyses.symbol,
                            analyses.completed_at DESC NULLS LAST,
                            analyses.requested_at DESC
                    )
                    SELECT
                        clusters.id AS event_cluster_id,
                        clusters.cluster_key,
                        clusters.primary_symbol AS symbol,
                        clusters.first_published_at,
                        clusters.last_published_at,
                        clusters.status AS cluster_status,
                        freshness.latest_ingested_at,
                        analysis.id AS ai_analysis_id,
                        analysis.direction,
                        analysis.confidence,
                        analysis.raw_response,
                        analysis.completed_at,
                        (
                            analysis.id IS NOT NULL
                            AND analysis.completed_at IS NOT NULL
                            AND analysis.completed_at
                                >= freshness.latest_ingested_at
                        ) AS analysis_is_fresh
                    FROM event_clusters clusters
                    JOIN cluster_freshness freshness
                        ON freshness.event_cluster_id = clusters.id
                    LEFT JOIN latest_completed_analysis analysis
                        ON analysis.event_cluster_id = clusters.id
                       AND analysis.symbol = clusters.primary_symbol
                    ORDER BY
                        clusters.primary_symbol,
                        clusters.last_published_at
                    """,
                    (
                        normalized_symbols,
                        published_after,
                        current_time,
                        PROMPT_VERSION,
                        normalized_symbols,
                    ),
                )
                rows = cursor.fetchall()

        result = {symbol: [] for symbol in normalized_symbols}

        for row in rows:
            raw_response = row["raw_response"]

            if not isinstance(raw_response, dict):
                raw_response = {}

            analysis_is_fresh = bool(row["analysis_is_fresh"])
            required_ai_fields_present = all(
                key in raw_response
                for key in (
                    "meaningful_company_specific_catalyst",
                    "sufficient_news",
                )
            )
            processed = (
                analysis_is_fresh
                and required_ai_fields_present
                and row["direction"]
                in {"bullish", "bearish", "neutral"}
                and row["confidence"] is not None
            )

            result[row["symbol"]].append(
                NewsAnalysis(
                    cluster_key=row["cluster_key"],
                    published_at=row["last_published_at"],
                    processed=processed,
                    direction=(
                        row["direction"] if processed else None
                    ),
                    confidence=(
                        float(row["confidence"])
                        if processed
                        else None
                    ),
                    meaningful_company_specific_catalyst=(
                        bool(
                            raw_response[
                                "meaningful_company_specific_catalyst"
                            ]
                        )
                        if processed
                        else None
                    ),
                    sufficient_news=(
                        bool(raw_response["sufficient_news"])
                        if processed
                        else None
                    ),
                    event_cluster_id=row["event_cluster_id"],
                    ai_analysis_id=(
                        row["ai_analysis_id"]
                        if processed
                        else None
                    ),
                )
            )

        return result

    def load_current_session_context(
        self,
        symbols,
        previous_session_close,
        now=None,
    ):
        return self.load_for_symbols(
            symbols=symbols,
            published_after=previous_session_close,
            now=now,
        )

    def health_check(self):
        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*)
                         FROM source_events
                         WHERE source = 'alpaca_news')
                            AS alpaca_source_events,
                        (SELECT COUNT(*)
                         FROM event_clusters
                         WHERE event_type = 'news')
                            AS news_clusters,
                        (SELECT COUNT(*)
                         FROM ai_analyses
                         WHERE event_cluster_id IS NOT NULL
                           AND status = 'completed'
                           AND prompt_version = %s)
                            AS completed_cluster_analyses
                    """,
                    (PROMPT_VERSION,),
                )
                counts = cursor.fetchone()

                cursor.execute(
                    """
                    WITH latest_event_ingestion AS (
                        SELECT
                            clusters.id AS event_cluster_id,
                            clusters.primary_symbol AS symbol,
                            clusters.cluster_key,
                            MAX(events.ingested_at) AS latest_ingested_at
                        FROM event_clusters clusters
                        JOIN event_cluster_members members
                            ON members.event_cluster_id = clusters.id
                        JOIN source_events events
                            ON events.id = members.source_event_id
                        WHERE clusters.event_type = 'news'
                        GROUP BY
                            clusters.id,
                            clusters.primary_symbol,
                            clusters.cluster_key
                    ),
                    latest_analysis AS (
                        SELECT DISTINCT ON (
                            analyses.event_cluster_id,
                            analyses.symbol
                        )
                            analyses.event_cluster_id,
                            analyses.symbol,
                            analyses.completed_at,
                            analyses.raw_response
                        FROM ai_analyses analyses
                        WHERE analyses.status = 'completed'
                          AND analyses.prompt_version = %s
                          AND analyses.event_cluster_id IS NOT NULL
                        ORDER BY
                            analyses.event_cluster_id,
                            analyses.symbol,
                            analyses.completed_at DESC NULLS LAST
                    )
                    SELECT COUNT(*) AS clusters_requiring_analysis
                    FROM latest_event_ingestion ingestion
                    LEFT JOIN latest_analysis analysis
                        ON analysis.event_cluster_id
                            = ingestion.event_cluster_id
                       AND analysis.symbol = ingestion.symbol
                    WHERE analysis.event_cluster_id IS NULL
                       OR analysis.completed_at
                            < ingestion.latest_ingested_at
                       OR NOT COALESCE(
                            analysis.raw_response
                                ? 'meaningful_company_specific_catalyst'
                            AND analysis.raw_response
                                ? 'sufficient_news',
                            false
                       )
                    """,
                    (PROMPT_VERSION,),
                )
                pending = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT
                        clusters.primary_symbol AS symbol,
                        clusters.cluster_key,
                        clusters.last_published_at,
                        analyses.direction,
                        analyses.confidence,
                        analyses.raw_response ->>
                            'meaningful_company_specific_catalyst'
                            AS meaningful,
                        analyses.raw_response ->>
                            'sufficient_news'
                            AS sufficient
                    FROM event_clusters clusters
                    LEFT JOIN LATERAL (
                        SELECT
                            analysis.direction,
                            analysis.confidence,
                            analysis.raw_response
                        FROM ai_analyses analysis
                        WHERE analysis.event_cluster_id = clusters.id
                          AND analysis.status = 'completed'
                          AND analysis.prompt_version = %s
                        ORDER BY analysis.completed_at DESC NULLS LAST
                        LIMIT 1
                    ) analyses ON true
                    WHERE clusters.event_type = 'news'
                    ORDER BY clusters.last_published_at DESC
                    LIMIT 5
                    """,
                    (PROMPT_VERSION,),
                )
                latest_clusters = cursor.fetchall()

        return {
            "prompt_version": PROMPT_VERSION,
            "counts": dict(counts),
            "clusters_requiring_analysis": pending[
                "clusters_requiring_analysis"
            ],
            "latest_clusters": [
                {
                    "symbol": row["symbol"],
                    "cluster_key": row["cluster_key"],
                    "last_published_at": row[
                        "last_published_at"
                    ],
                    "direction": row["direction"],
                    "confidence": (
                        float(row["confidence"])
                        if row["confidence"] is not None
                        else None
                    ),
                    "meaningful": row["meaningful"],
                    "sufficient": row["sufficient"],
                }
                for row in latest_clusters
            ],
            "obsolete_stored_deterministic_gates_used": False,
            "writes_performed": False,
        }


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    return str(value)


def parse_args():
    parser = argparse.ArgumentParser(
        description="DELTAX production news-context repository."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run a read-only news-context health check.",
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

    repository = NewsContextRepository()
    print(
        json.dumps(
            repository.health_check(),
            indent=2,
            ensure_ascii=False,
            default=json_default,
        )
    )
    print("NEWS CONTEXT HEALTH CHECK: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
