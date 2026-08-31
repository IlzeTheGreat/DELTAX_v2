# File: helpers/test_ai_analysis_persistence.py
# Purpose: Analyzes and persists the latest news cluster for each symbol without duplicating completed AI analyses.

import hashlib
import json
import os
import sys

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv

from test_ai_news_cluster_analysis import (
    analyze_cluster,
    apply_deterministic_gates,
)


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

PROMPT_VERSION = "deltax_news_cluster_v1"
DEFAULT_SYMBOLS = ["IREN", "PCG", "RKLB", "CW"]


def load_latest_clusters(symbols):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH latest_clusters AS (
                    SELECT DISTINCT ON (primary_symbol)
                        id,
                        primary_symbol,
                        cluster_key,
                        first_published_at,
                        last_published_at
                    FROM event_clusters
                    WHERE primary_symbol = ANY(%s)
                      AND event_type = 'news'
                    ORDER BY
                        primary_symbol,
                        last_published_at DESC,
                        created_at DESC
                )
                SELECT
                    latest.id,
                    latest.primary_symbol,
                    latest.cluster_key,
                    latest.first_published_at,
                    latest.last_published_at,
                    events.id,
                    events.external_id,
                    events.headline,
                    COALESCE(events.summary, ''),
                    COALESCE(events.content, ''),
                    events.published_at
                FROM latest_clusters latest
                JOIN event_cluster_members members
                    ON members.event_cluster_id = latest.id
                JOIN source_events events
                    ON events.id = members.source_event_id
                ORDER BY
                    latest.primary_symbol,
                    events.published_at
                """,
                (symbols,),
            )

            rows = cursor.fetchall()

    clusters = {}

    for row in rows:
        cluster_id = row[0]

        if cluster_id not in clusters:
            clusters[cluster_id] = {
                "id": cluster_id,
                "symbol": row[1],
                "cluster_key": row[2],
                "first_published_at": row[3],
                "last_published_at": row[4],
                "events": [],
            }

        clusters[cluster_id]["events"].append(
            {
                "id": row[5],
                "external_id": row[6],
                "headline": row[7],
                "summary": row[8],
                "content": row[9],
                "published_at": row[10],
            }
        )

    return list(clusters.values())


def calculate_input_hash(cluster):
    payload = {
        "cluster_key": cluster["cluster_key"],
        "events": [
            {
                "id": str(event["id"]),
                "external_id": event["external_id"],
                "headline": event["headline"],
                "summary": event["summary"],
                "content": event["content"],
                "published_at": event["published_at"].isoformat(),
            }
            for event in sorted(
                cluster["events"],
                key=lambda item: (
                    item["published_at"],
                    str(item["id"]),
                ),
            )
        ],
    }

    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


def map_time_horizon(value):
    mapping = {
        "intraday": "intraday",
        "several_days": "active",
        "several_weeks": "core",
        "unclear": "unknown",
    }

    return mapping.get(value, "unknown")


def calculate_impact_score(direction, confidence):
    score = round(confidence * 100)

    if direction == "bullish":
        return score

    if direction == "bearish":
        return -score

    return 0


def calculate_trade_relevance(analysis):
    confidence = float(analysis["confidence"])
    meaningful = bool(
        analysis["meaningful_company_specific_catalyst"]
    )
    sufficient = bool(analysis["sufficient_news"])

    if meaningful and sufficient:
        return confidence

    return min(confidence, 0.30)


def find_existing_analysis(
    cluster_id,
    symbol,
    input_hash,
):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    status,
                    raw_response
                FROM ai_analyses
                WHERE event_cluster_id = %s
                  AND symbol = %s
                  AND model = %s
                  AND prompt_version = %s
                  AND input_hash = %s
                LIMIT 1
                """,
                (
                    cluster_id,
                    symbol,
                    OPENAI_MODEL,
                    PROMPT_VERSION,
                    input_hash,
                ),
            )

            return cursor.fetchone()


def persist_analysis(
    cluster,
    input_hash,
    analysis,
    gates,
):
    confidence = float(analysis["confidence"])
    direction = analysis["direction"]

    raw_response = {
        **analysis,
        "deterministic_gates": gates,
    }

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ai_analyses (
                    event_cluster_id,
                    symbol,
                    model,
                    prompt_version,
                    input_hash,
                    status,
                    event_type,
                    direction,
                    impact_score,
                    confidence,
                    time_horizon,
                    trade_relevance,
                    catalyst,
                    facts,
                    risks,
                    invalidation_condition,
                    earnings_metrics,
                    raw_response,
                    completed_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    'completed',
                    'news_cluster',
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    '{}'::jsonb,
                    %s,
                    now()
                )
                ON CONFLICT (
                    event_cluster_id,
                    symbol,
                    model,
                    prompt_version,
                    input_hash
                )
                WHERE event_cluster_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                (
                    cluster["id"],
                    cluster["symbol"],
                    OPENAI_MODEL,
                    PROMPT_VERSION,
                    input_hash,
                    direction,
                    calculate_impact_score(
                        direction,
                        confidence,
                    ),
                    confidence,
                    map_time_horizon(
                        analysis.get("time_horizon")
                    ),
                    calculate_trade_relevance(analysis),
                    analysis.get("catalyst"),
                    Jsonb(
                        analysis.get(
                            "evidence_headlines",
                            [],
                        )
                    ),
                    Jsonb(analysis.get("risks", [])),
                    analysis.get(
                        "invalidation_condition"
                    ),
                    Jsonb(raw_response),
                ),
            )

            inserted = cursor.fetchone()
            connection.commit()

    return inserted[0] if inserted else None


def count_saved_analyses(cluster_ids):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM ai_analyses
                WHERE event_cluster_id = ANY(%s)
                  AND model = %s
                  AND prompt_version = %s
                  AND status = 'completed'
                """,
                (
                    cluster_ids,
                    OPENAI_MODEL,
                    PROMPT_VERSION,
                ),
            )

            return cursor.fetchone()[0]


def main():
    symbols = [symbol.upper() for symbol in sys.argv[1:]]

    if not symbols:
        symbols = DEFAULT_SYMBOLS

    clusters = load_latest_clusters(symbols)

    print(f"Latest clusters loaded: {len(clusters)}")

    created = 0
    reused = 0

    for cluster in sorted(
        clusters,
        key=lambda item: item["symbol"],
    ):
        symbol = cluster["symbol"]
        input_hash = calculate_input_hash(cluster)

        existing = find_existing_analysis(
            cluster["id"],
            symbol,
            input_hash,
        )

        if existing and existing[1] == "completed":
            reused += 1
            raw_response = existing[2] or {}
            analysis = raw_response
            gates = raw_response.get(
                "deterministic_gates",
                {},
            )

            print(f"\n{symbol}: existing analysis reused")
        else:
            print(
                f"\n{symbol}: analyzing "
                f"{len(cluster['events'])} articles"
            )

            analysis = analyze_cluster(cluster)
            gates = apply_deterministic_gates(analysis)

            analysis_id = persist_analysis(
                cluster,
                input_hash,
                analysis,
                gates,
            )

            if analysis_id:
                created += 1
                print(f"Analysis saved: {analysis_id}")
            else:
                reused += 1
                print("Analysis already existed")

        print(
            f"Direction: {analysis.get('direction')} | "
            f"confidence={analysis.get('confidence')}"
        )
        print(
            f"STOCK: {gates.get('stock_gate')} | "
            f"{gates.get('stock_reason')}"
        )
        print(
            f"OPTIONS: {gates.get('options_gate')} | "
            f"{gates.get('options_reason')}"
        )

    cluster_ids = [
        cluster["id"]
        for cluster in clusters
    ]

    saved = count_saved_analyses(cluster_ids)

    print("\nPERSISTENCE SUMMARY")
    print(f"Analyses created: {created}")
    print(f"Analyses reused: {reused}")
    print(f"Completed analyses stored: {saved}")

    assert saved == len(clusters), (
        f"Expected {len(clusters)} completed analyses, "
        f"found {saved}"
    )

    print("\nAI ANALYSIS PERSISTENCE TEST: OK")


if __name__ == "__main__":
    main()