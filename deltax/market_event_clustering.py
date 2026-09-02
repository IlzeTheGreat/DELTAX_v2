# File: deltax/market_event_clustering.py
# Purpose: Groups unclustered Finnhub and Marketaux macro news before AI analysis.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb


CLUSTERING_VERSION = "deltax_market_cluster_v1"
MARKET_SOURCES = ("alpaca_news", "finnhub_news", "marketaux_news")
DEFAULT_LOOKBACK_HOURS = 48
DEFAULT_MAX_EVENTS = 100
MAX_CLUSTER_GAP_MINUTES = 360
SIMILARITY_WITH_SHARED_RISK = 0.20
SIMILARITY_WITHOUT_SHARED_RISK = 0.55

STOP_WORDS = {
    "about", "after", "again", "against", "amid", "among", "and", "are",
    "before", "being", "between", "could", "from", "have", "into", "latest",
    "more", "new", "over", "report", "reports", "says", "said", "that", "the",
    "their", "this", "under", "update", "was", "were", "will", "with", "would",
}


@dataclass(frozen=True)
class SourceEvent:
    id: Any
    source: str
    external_id: str
    headline: str
    summary: str
    source_url: str
    published_at: datetime
    content_hash: str
    risks: frozenset[str]
    tokens: frozenset[str]


@dataclass
class ClusterState:
    id: Any | None
    cluster_key: str
    first_published_at: datetime
    last_published_at: datetime
    risks: set[str]
    representative_tokens: set[str]
    content_hashes: set[str]
    sources: set[str]
    member_count: int
    new_events: list[SourceEvent] = field(default_factory=list)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DELTAX market-event clustering")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Validate schema and show counts")
    mode.add_argument("--dry-run", action="store_true", help="Preview assignments without writes")
    mode.add_argument("--apply", action="store_true", help="Persist market clusters and memberships")
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    return parser.parse_args()


def tokenize(*parts: str) -> frozenset[str]:
    text = " ".join(parts).lower()
    words = re.findall(r"[a-z0-9]+", text)
    return frozenset(
        word
        for word in words
        if len(word) >= 3 and word not in STOP_WORDS and not word.isdigit()
    )


def parse_risks(raw_payload: Any) -> frozenset[str]:
    if not isinstance(raw_payload, dict):
        return frozenset({"market_risk"})
    values = raw_payload.get("deltax_matched_risks")
    if not isinstance(values, (list, tuple)):
        return frozenset({"market_risk"})
    risks = {
        str(value).strip().lower()
        for value in values
        if str(value).strip()
    }
    return frozenset(risks or {"market_risk"})


def safe_string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if str(item).strip()}


def jaccard(left: set[str] | frozenset[str], right: set[str] | frozenset[str]) -> float:
    union = set(left) | set(right)
    if not union:
        return 0.0
    return len(set(left) & set(right)) / len(union)


def time_gap_minutes(event: SourceEvent, cluster: ClusterState) -> float:
    if event.published_at < cluster.first_published_at:
        gap = cluster.first_published_at - event.published_at
    elif event.published_at > cluster.last_published_at:
        gap = event.published_at - cluster.last_published_at
    else:
        return 0.0
    return gap.total_seconds() / 60.0


def match_score(event: SourceEvent, cluster: ClusterState) -> float | None:
    if event.content_hash and event.content_hash in cluster.content_hashes:
        return 2.0
    if time_gap_minutes(event, cluster) > MAX_CLUSTER_GAP_MINUTES:
        return None

    similarity = jaccard(event.tokens, cluster.representative_tokens)
    shared_risks = event.risks & cluster.risks

    # "etf_symbol_news" is only an ingestion label, not a real event type.
    # Do not let it by itself lower the similarity threshold, otherwise
    # unrelated ETF-tagged stories can be merged into one cluster.
    meaningful_shared_risks = shared_risks - {"etf_symbol_news"}

    if meaningful_shared_risks and similarity >= SIMILARITY_WITH_SHARED_RISK:
        return 1.0 + similarity

    if similarity >= SIMILARITY_WITHOUT_SHARED_RISK:
        return similarity
    return None


def stable_cluster_key(event: SourceEvent) -> str:
    bucket_minute = (event.published_at.minute // 15) * 15
    bucket = event.published_at.replace(
        minute=bucket_minute,
        second=0,
        microsecond=0,
    )
    primary_risk = sorted(event.risks)[0]
    risk_slug = re.sub(r"[^a-z0-9]+", "_", primary_risk).strip("_")[:40]
    fingerprint = hashlib.sha256(
        f"{event.headline.lower()}|{event.content_hash}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{CLUSTERING_VERSION}:{risk_slug}:{bucket:%Y%m%dT%H%MZ}:{fingerprint}"


def validate_schema(connection: psycopg.Connection[Any]) -> None:
    required = {
        "event_clusters": {
            "id", "cluster_key", "primary_symbol", "event_type", "status",
            "first_published_at", "last_published_at", "scope",
            "analysis_status", "analysis_metadata",
        },
        "event_cluster_members": {"event_cluster_id", "source_event_id"},
        "source_events": {
            "id", "source", "external_id", "source_type", "headline", "summary",
            "source_url", "published_at", "content_hash", "raw_payload",
        },
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
            """,
            (list(required),),
        )
        actual: dict[str, set[str]] = {}
        for table_name, column_name in cursor.fetchall():
            actual.setdefault(table_name, set()).add(column_name)
    missing = {
        table: sorted(columns - actual.get(table, set()))
        for table, columns in required.items()
        if columns - actual.get(table, set())
    }
    if missing:
        raise RuntimeError(f"Database schema is missing required columns: {missing}")


def load_unclustered_events(
    connection: psycopg.Connection[Any],
    cutoff: datetime,
    max_events: int,
) -> list[SourceEvent]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                events.id,
                events.source,
                events.external_id,
                COALESCE(events.headline, ''),
                COALESCE(events.summary, ''),
                COALESCE(events.source_url, ''),
                events.published_at,
                COALESCE(events.content_hash, ''),
                events.raw_payload
            FROM source_events events
            WHERE events.source = ANY(%s)
              AND events.source_type = 'market_news'
              AND events.published_at >= %s
              AND NOT EXISTS (
                  SELECT 1
                  FROM event_cluster_members members
                  WHERE members.source_event_id = events.id
              )
            ORDER BY events.published_at, events.id
            LIMIT %s
            """,
            (list(MARKET_SOURCES), cutoff, max_events),
        )
        rows = cursor.fetchall()

    return [
        SourceEvent(
            id=row[0],
            source=row[1],
            external_id=row[2],
            headline=row[3],
            summary=row[4],
            source_url=row[5],
            published_at=row[6],
            content_hash=row[7],
            risks=parse_risks(row[8]),
            tokens=tokenize(row[3], row[4]),
        )
        for row in rows
    ]


def load_open_market_clusters(
    connection: psycopg.Connection[Any],
    cutoff: datetime,
) -> list[ClusterState]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                clusters.id,
                clusters.cluster_key,
                clusters.first_published_at,
                clusters.last_published_at,
                clusters.analysis_metadata,
                count(members.source_event_id)
            FROM event_clusters clusters
            LEFT JOIN event_cluster_members members
              ON members.event_cluster_id = clusters.id
            WHERE clusters.scope = 'market'
              AND clusters.status = 'open'
              AND clusters.last_published_at >= %s
            GROUP BY clusters.id
            ORDER BY clusters.last_published_at
            """,
            (cutoff - timedelta(minutes=MAX_CLUSTER_GAP_MINUTES),),
        )
        rows = cursor.fetchall()

    clusters: list[ClusterState] = []
    for row in rows:
        metadata = row[4] if isinstance(row[4], dict) else {}
        clusters.append(
            ClusterState(
                id=row[0],
                cluster_key=row[1],
                first_published_at=row[2],
                last_published_at=row[3],
                risks=safe_string_set(metadata.get("risk_labels")),
                representative_tokens=safe_string_set(
                    metadata.get("representative_tokens")
                ),
                content_hashes=safe_string_set(metadata.get("content_hashes")),
                sources=safe_string_set(metadata.get("sources")),
                member_count=int(row[5]),
            )
        )
    return clusters


def assign_events(
    events: list[SourceEvent],
    clusters: list[ClusterState],
) -> list[tuple[SourceEvent, ClusterState, str, float | None]]:
    assignments: list[tuple[SourceEvent, ClusterState, str, float | None]] = []
    for event in events:
        matches = [
            (score, cluster)
            for cluster in clusters
            if (score := match_score(event, cluster)) is not None
        ]
        if matches:
            score, selected = max(matches, key=lambda item: item[0])
            action = "join"
        else:
            score = None
            selected = ClusterState(
                id=None,
                cluster_key=stable_cluster_key(event),
                first_published_at=event.published_at,
                last_published_at=event.published_at,
                risks=set(event.risks),
                representative_tokens=set(event.tokens),
                content_hashes=set(),
                sources=set(),
                member_count=0,
            )
            clusters.append(selected)
            action = "new"

        selected.first_published_at = min(selected.first_published_at, event.published_at)
        selected.last_published_at = max(selected.last_published_at, event.published_at)
        selected.risks.update(event.risks)
        selected.content_hashes.add(event.content_hash)
        selected.sources.add(event.source)
        selected.member_count += 1
        selected.new_events.append(event)
        assignments.append((event, selected, action, score))
    return assignments


def cluster_metadata(cluster: ClusterState) -> dict[str, Any]:
    return {
        "clustering_version": CLUSTERING_VERSION,
        "risk_labels": sorted(cluster.risks),
        "representative_tokens": sorted(cluster.representative_tokens),
        "content_hashes": sorted(value for value in cluster.content_hashes if value),
        "sources": sorted(cluster.sources),
        "member_count": cluster.member_count,
    }


def persist_assignments(
    connection: psycopg.Connection[Any],
    clusters: list[ClusterState],
) -> dict[str, int]:
    counts = {"clusters_created": 0, "clusters_updated": 0, "members_added": 0}
    changed = [cluster for cluster in clusters if cluster.new_events]
    with connection.cursor() as cursor:
        for cluster in changed:
            metadata = Jsonb(cluster_metadata(cluster))
            if cluster.id is None:
                cursor.execute(
                    """
                    INSERT INTO event_clusters (
                        cluster_key,
                        primary_symbol,
                        event_type,
                        status,
                        first_published_at,
                        last_published_at,
                        scope,
                        analysis_status,
                        analysis_metadata,
                        updated_at
                    )
                    VALUES (
                        %s, NULL, 'market_news', 'open', %s, %s,
                        'market', 'pending', %s, now()
                    )
                    RETURNING id
                    """,
                    (
                        cluster.cluster_key,
                        cluster.first_published_at,
                        cluster.last_published_at,
                        metadata,
                    ),
                )
                cluster.id = cursor.fetchone()[0]
                counts["clusters_created"] += 1
            else:
                cursor.execute(
                    """
                    UPDATE event_clusters
                    SET first_published_at = LEAST(first_published_at, %s),
                        last_published_at = GREATEST(last_published_at, %s),
                        analysis_status = 'pending',
                        analysis_metadata = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        cluster.first_published_at,
                        cluster.last_published_at,
                        metadata,
                        cluster.id,
                    ),
                )
                counts["clusters_updated"] += cursor.rowcount

            for event in cluster.new_events:
                cursor.execute(
                    """
                    INSERT INTO event_cluster_members (event_cluster_id, source_event_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (cluster.id, event.id),
                )
                counts["members_added"] += cursor.rowcount
    return counts


def database_counts(connection: psycopg.Connection[Any], cutoff: datetime) -> dict[str, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM source_events events
            WHERE events.source = ANY(%s)
              AND events.source_type = 'market_news'
              AND events.published_at >= %s
              AND NOT EXISTS (
                  SELECT 1 FROM event_cluster_members members
                  WHERE members.source_event_id = events.id
              )
            """,
            (list(MARKET_SOURCES), cutoff),
        )
        unclustered = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM event_clusters WHERE scope = 'market'")
        market_clusters = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT count(*)
            FROM event_clusters
            WHERE scope = 'market' AND analysis_status = 'pending'
            """
        )
        pending = cursor.fetchone()[0]
    return {
        "unclustered_market_source_events": unclustered,
        "market_event_clusters": market_clusters,
        "pending_market_event_clusters": pending,
    }


def print_assignments(
    assignments: list[tuple[SourceEvent, ClusterState, str, float | None]],
) -> None:
    for index, (event, cluster, action, score) in enumerate(assignments, 1):
        print("-" * 78)
        print(f"Assignment {index}: {action.upper()}")
        print(f"Cluster: {cluster.cluster_key}")
        print(f"Match score: {score:.3f}" if score is not None else "Match score: n/a")
        print(f"Source: {event.source}")
        print(f"Risks: {', '.join(sorted(event.risks))}")
        print(f"Headline: {event.headline}")


def main() -> int:
    args = parse_args()
    load_dotenv(project_root() / ".env")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: DATABASE_URL is missing from .env.", file=sys.stderr)
        return 1
    if not 1 <= args.lookback_hours <= 168:
        print("ERROR: --lookback-hours must be between 1 and 168.", file=sys.stderr)
        return 1
    if not 1 <= args.max_events <= 1000:
        print("ERROR: --max-events must be between 1 and 1000.", file=sys.stderr)
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.lookback_hours)
    try:
        with psycopg.connect(database_url) as connection:
            validate_schema(connection)
            before = database_counts(connection, cutoff)
            if args.check:
                print(json.dumps({
                    "clustering_version": CLUSTERING_VERSION,
                    "market_sources": list(MARKET_SOURCES),
                    "lookback_hours": args.lookback_hours,
                    "max_cluster_gap_minutes": MAX_CLUSTER_GAP_MINUTES,
                    "counts": before,
                    "database_writes_performed": False,
                    "openai_requests_performed": 0,
                }, indent=2))
                print("MARKET EVENT CLUSTERING HEALTH CHECK: OK")
                return 0

            events = load_unclustered_events(connection, cutoff, args.max_events)
            clusters = load_open_market_clusters(connection, cutoff)
            assignments = assign_events(events, clusters)

            print("DELTAX MARKET EVENT CLUSTERING")
            print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
            print(f"Unclustered events loaded: {len(events)}")
            print(f"Assignments prepared: {len(assignments)}")
            print_assignments(assignments)

            if args.dry_run:
                connection.rollback()
                print("\nNo database writes were performed.")
                print("No OpenAI requests were performed.")
                print("MARKET EVENT CLUSTERING DRY RUN: OK")
                return 0

            counts = persist_assignments(connection, clusters)
            connection.commit()
            after = database_counts(connection, cutoff)
            print("\n" + json.dumps({"changes": counts, "counts_after": after}, indent=2))
            print("No OpenAI requests were performed.")
            print("No trade theses, intents, or orders were created.")
            print("MARKET EVENT CLUSTERING APPLY: OK")
            return 0
    except (RuntimeError, psycopg.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
