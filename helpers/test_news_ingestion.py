# File: helpers/test_news_ingestion.py
# Purpose: Fetches recent Alpaca news for selected symbols and persists original publication timestamps, content, and symbol links in Neon.

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.types.json import Jsonb


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
DEFAULT_SYMBOLS = ["IREN", "PCG", "RKLB", "CW"]
NEWS_LOOKBACK_DAYS = 7
PAGE_SIZE = 50
MAX_PAGES = 10


def parse_timestamp(value: str | None):
    if not value:
        return None

    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def calculate_content_hash(article: dict) -> str:
    content = "|".join(
        [
            str(article.get("headline") or ""),
            str(article.get("summary") or ""),
            str(article.get("content") or ""),
            str(article.get("url") or ""),
        ]
    )

    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


def fetch_news(symbols: list[str]) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(
        days=NEWS_LOOKBACK_DAYS
    )

    articles_by_id = {}
    page_token = None

    for page_number in range(1, MAX_PAGES + 1):
        params = {
            "symbols": ",".join(symbols),
            "start": start.isoformat(),
            "limit": PAGE_SIZE,
            "sort": "desc",
            "include_content": "true",
            "exclude_contentless": "false",
        }

        if page_token:
            params["page_token"] = page_token

        response = requests.get(
            ALPACA_NEWS_URL,
            headers={
                "APCA-API-KEY-ID": os.environ[
                    "ALPACA_API_KEY_PAPER"
                ],
                "APCA-API-SECRET-KEY": os.environ[
                    "ALPACA_API_SECRET_PAPER"
                ],
            },
            params=params,
            timeout=30,
        )

        response.raise_for_status()
        payload = response.json()

        page_articles = payload.get("news", [])

        print(
            f"Page {page_number}: "
            f"{len(page_articles)} articles"
        )

        for article in page_articles:
            content_hash = calculate_content_hash(article)
            external_id = str(
                article.get("id") or content_hash
            )
            articles_by_id[external_id] = article

        page_token = payload.get("next_page_token")

        if not page_token or not page_articles:
            break

    return list(articles_by_id.values())


def load_symbol_map(
    cursor,
    requested_symbols: list[str],
) -> dict[str, str]:
    cursor.execute(
        """
        SELECT symbol, alpaca_symbol
        FROM instruments
        WHERE alpaca_symbol = ANY(%s);
        """,
        (requested_symbols,),
    )

    return {
        alpaca_symbol: database_symbol
        for database_symbol, alpaca_symbol
        in cursor.fetchall()
    }


def save_article(
    cursor,
    article: dict,
    symbol_map: dict[str, str],
) -> tuple[str, int]:
    content_hash = calculate_content_hash(article)
    external_id = str(
        article.get("id") or content_hash
    )

    published_at = parse_timestamp(
        article.get("created_at")
    )

    if published_at is None:
        raise RuntimeError(
            f"Article {external_id} has no created_at"
        )

    source_updated_at = parse_timestamp(
        article.get("updated_at")
    )

    cursor.execute(
        """
        INSERT INTO source_events (
            source,
            external_id,
            source_type,
            headline,
            summary,
            content,
            source_url,
            published_at,
            source_updated_at,
            content_hash,
            processing_status,
            raw_payload
        )
        VALUES (
            'alpaca_news',
            %s,
            'news',
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            'pending',
            %s
        )
        ON CONFLICT (source, external_id)
        DO UPDATE SET
            headline = EXCLUDED.headline,
            summary = EXCLUDED.summary,
            content = EXCLUDED.content,
            source_url = EXCLUDED.source_url,
            published_at = LEAST(
                source_events.published_at,
                EXCLUDED.published_at
            ),
            source_updated_at = COALESCE(
                EXCLUDED.source_updated_at,
                source_events.source_updated_at
            ),
            content_hash = EXCLUDED.content_hash,
            raw_payload = EXCLUDED.raw_payload
        RETURNING id;
        """,
        (
            external_id,
            article.get("headline"),
            article.get("summary"),
            article.get("content"),
            article.get("url"),
            published_at,
            source_updated_at,
            content_hash,
            Jsonb(article),
        ),
    )

    source_event_id = cursor.fetchone()[0]
    linked_symbols = 0

    for alpaca_symbol in article.get("symbols", []):
        database_symbol = symbol_map.get(alpaca_symbol)

        if database_symbol is None:
            continue

        cursor.execute(
            """
            INSERT INTO source_event_symbols (
                source_event_id,
                symbol
            )
            VALUES (%s, %s)
            ON CONFLICT (
                source_event_id,
                symbol
            )
            DO NOTHING;
            """,
            (
                source_event_id,
                database_symbol,
            ),
        )

        linked_symbols += cursor.rowcount

    return str(source_event_id), linked_symbols


def print_database_summary(cursor) -> None:
    cursor.execute(
        """
        SELECT
            COUNT(*),
            MIN(published_at),
            MAX(published_at)
        FROM source_events
        WHERE source = 'alpaca_news';
        """
    )

    event_count, oldest, newest = cursor.fetchone()

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM source_event_symbols ses
        JOIN source_events se
            ON se.id = ses.source_event_id
        WHERE se.source = 'alpaca_news';
        """
    )

    link_count = cursor.fetchone()[0]

    print("\nDATABASE SUMMARY")
    print(f"Stored Alpaca news events: {event_count}")
    print(f"Stored symbol links: {link_count}")
    print(f"Oldest published_at: {oldest}")
    print(f"Newest published_at: {newest}")

    cursor.execute(
        """
        SELECT
            se.external_id,
            se.published_at,
            se.ingested_at,
            se.headline,
            ARRAY_AGG(
                ses.symbol
                ORDER BY ses.symbol
            ) FILTER (
                WHERE ses.symbol IS NOT NULL
            ) AS symbols
        FROM source_events se
        LEFT JOIN source_event_symbols ses
            ON ses.source_event_id = se.id
        WHERE se.source = 'alpaca_news'
        GROUP BY
            se.id,
            se.external_id,
            se.published_at,
            se.ingested_at,
            se.headline
        ORDER BY se.published_at DESC
        LIMIT 10;
        """
    )

    print("\nLATEST STORED NEWS")

    for (
        external_id,
        published_at,
        ingested_at,
        headline,
        symbols,
    ) in cursor.fetchall():
        print(
            f"{published_at} | "
            f"{symbols or []} | "
            f"{external_id} | "
            f"{headline}"
        )
        print(f"  ingested_at={ingested_at}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "symbols",
        nargs="*",
        default=DEFAULT_SYMBOLS,
    )

    args = parser.parse_args()

    symbols = sorted(
        {
            symbol.upper()
            for symbol in args.symbols
        }
    )

    print("Requested symbols: " + ", ".join(symbols))

    articles = fetch_news(symbols)

    print(f"Unique articles fetched: {len(articles)}")

    with psycopg.connect(
        os.environ["DATABASE_URL"],
        connect_timeout=10,
    ) as connection:
        with connection.cursor() as cursor:
            symbol_map = load_symbol_map(
                cursor,
                symbols,
            )

            missing_symbols = (
                set(symbols) - set(symbol_map)
            )

            if missing_symbols:
                raise RuntimeError(
                    "Symbols missing from instruments: "
                    + ", ".join(sorted(missing_symbols))
                )

            saved_events = 0
            created_links = 0

            for article in articles:
                save_article(
                    cursor=cursor,
                    article=article,
                    symbol_map=symbol_map,
                )

                saved_events += 1

            # Count links separately after all idempotent upserts.
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM source_event_symbols ses
                JOIN source_events se
                    ON se.id = ses.source_event_id
                WHERE se.source = 'alpaca_news'
                  AND ses.symbol = ANY(%s);
                """,
                (list(symbol_map.values()),),
            )

            created_links = cursor.fetchone()[0]

            print(f"Events upserted: {saved_events}")
            print(
                f"Relevant symbol links in database: "
                f"{created_links}"
            )

            print_database_summary(cursor)

    print("\nNEWS INGESTION TEST: OK")