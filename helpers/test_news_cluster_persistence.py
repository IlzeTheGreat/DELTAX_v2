# File: helpers/test_news_cluster_persistence.py
# Purpose: Persists deterministic news clusters and their source-event memberships in Neon without creating duplicates.

import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from dotenv import load_dotenv

from test_news_event_clusters import (
    create_clusters,
    load_calendar,
    load_news,
    resolve_anchor,
)


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
DEFAULT_SYMBOLS = ["IREN", "PCG", "RKLB", "CW"]


def build_clusters(symbols):
    news = load_news(symbols)

    if not news:
        raise RuntimeError("No stored Alpaca news found")

    oldest = min(event["published_at"] for event in news)
    newest = max(event["published_at"] for event in news)

    sessions = load_calendar(
        oldest.date() - timedelta(days=3),
        newest.date() + timedelta(days=10),
    )

    anchored_events = []

    for event in news:
        anchor = resolve_anchor(event["published_at"], sessions)

        if anchor is None:
            raise RuntimeError(
                f"Could not resolve anchor for event {event['id']}"
            )

        anchored_events.append({**event, **anchor})

    return news, create_clusters(anchored_events)


def make_cluster_key(cluster):
    anchor_utc = cluster["anchor"].astimezone(timezone.utc)

    return (
        f"deltax_news_v1:"
        f"{cluster['symbol']}:"
        f"{cluster['anchor_type']}:"
        f"{anchor_utc.strftime('%Y%m%dT%H%M%SZ')}"
    )


def persist_clusters(clusters):
    now = datetime.now(timezone.utc)

    created_clusters = 0
    existing_clusters = 0
    inserted_members = 0
    cluster_keys = []

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            for cluster in clusters:
                cluster_key = make_cluster_key(cluster)
                cluster_keys.append(cluster_key)

                first_published_at = min(
                    event["published_at"]
                    for event in cluster["events"]
                )
                last_published_at = max(
                    event["published_at"]
                    for event in cluster["events"]
                )

                confirmation_due = cluster[
                    "confirmation_due"
                ].astimezone(timezone.utc)

                status = (
                    "open"
                    if confirmation_due > now
                    else "closed"
                )

                cursor.execute(
                    """
                    SELECT id
                    FROM event_clusters
                    WHERE cluster_key = %s
                    """,
                    (cluster_key,),
                )
                existing = cursor.fetchone()

                if existing:
                    cluster_id = existing[0]
                    existing_clusters += 1

                    cursor.execute(
                        """
                        UPDATE event_clusters
                        SET
                            first_published_at = %s,
                            last_published_at = %s,
                            event_type = 'news',
                            status = %s,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            first_published_at,
                            last_published_at,
                            status,
                            cluster_id,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO event_clusters (
                            cluster_key,
                            primary_symbol,
                            event_type,
                            status,
                            first_published_at,
                            last_published_at
                        )
                        VALUES (%s, %s, 'news', %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            cluster_key,
                            cluster["symbol"],
                            status,
                            first_published_at,
                            last_published_at,
                        ),
                    )

                    cluster_id = cursor.fetchone()[0]
                    created_clusters += 1

                for event in cluster["events"]:
                    cursor.execute(
                        """
                        INSERT INTO event_cluster_members (
                            event_cluster_id,
                            source_event_id
                        )
                        VALUES (%s, %s)
                        ON CONFLICT (
                            event_cluster_id,
                            source_event_id
                        )
                        DO NOTHING
                        """,
                        (cluster_id, event["id"]),
                    )

                    inserted_members += cursor.rowcount

        connection.commit()

    return {
        "created_clusters": created_clusters,
        "existing_clusters": existing_clusters,
        "inserted_members": inserted_members,
        "cluster_keys": cluster_keys,
    }


def verify_persistence(cluster_keys):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM event_clusters
                WHERE cluster_key = ANY(%s)
                """,
                (cluster_keys,),
            )
            stored_clusters = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM event_cluster_members members
                JOIN event_clusters clusters
                    ON clusters.id = members.event_cluster_id
                WHERE clusters.cluster_key = ANY(%s)
                """,
                (cluster_keys,),
            )
            stored_members = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT
                    clusters.primary_symbol,
                    clusters.cluster_key,
                    clusters.status,
                    clusters.first_published_at,
                    clusters.last_published_at,
                    COUNT(members.source_event_id)
                FROM event_clusters clusters
                LEFT JOIN event_cluster_members members
                    ON members.event_cluster_id = clusters.id
                WHERE clusters.cluster_key = ANY(%s)
                GROUP BY clusters.id
                ORDER BY clusters.last_published_at DESC
                """,
                (cluster_keys,),
            )

            rows = cursor.fetchall()

    return stored_clusters, stored_members, rows


def main():
    symbols = [symbol.upper() for symbol in sys.argv[1:]]

    if not symbols:
        symbols = DEFAULT_SYMBOLS

    news, clusters = build_clusters(symbols)

    print(f"Stored news events loaded: {len(news)}")
    print(f"Calculated clusters: {len(clusters)}")

    result = persist_clusters(clusters)

    stored_clusters, stored_members, rows = verify_persistence(
        result["cluster_keys"]
    )

    print("\nPERSISTENCE SUMMARY")
    print(f"Clusters created: {result['created_clusters']}")
    print(f"Existing clusters updated: {result['existing_clusters']}")
    print(f"New member links inserted: {result['inserted_members']}")
    print(f"Clusters stored: {stored_clusters}")
    print(f"Member links stored: {stored_members}")

    print("\nSTORED CLUSTERS")

    for row in rows:
        print(
            f"{row[0]} | status={row[2]} | "
            f"articles={row[5]} | "
            f"{row[3]} -> {row[4]}"
        )
        print(f"  {row[1]}")

    assert stored_clusters == len(clusters), (
        f"Expected {len(clusters)} clusters, "
        f"found {stored_clusters}"
    )

    assert stored_members == len(news), (
        f"Expected {len(news)} member links, "
        f"found {stored_members}"
    )

    print("\nNEWS CLUSTER PERSISTENCE TEST: OK")


if __name__ == "__main__":
    main()