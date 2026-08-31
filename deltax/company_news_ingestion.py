# File: deltax/company_news_ingestion.py
# Purpose: Production Alpaca/Benzinga company-news ingestion + deterministic
# company-news clustering for the DELTAX base universe.
#
# Modes:
#   --check   : schema/config health check only. No remote requests, no writes.
#   --dry-run : fetch Alpaca news and show counts. No writes.
#   --apply   : fetch, persist source_events/source_event_symbols, then rebuild
#               idempotent company-news clusters for the requested lookback.
#
# This module performs NO OpenAI requests, creates NO trade theses/intents,
# and submits NO broker orders.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import requests
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]

ALPACA_NEWS_URL = os.getenv(
    "ALPACA_NEWS_URL",
    "https://data.alpaca.markets/v1beta1/news",
).rstrip("/")

ALPACA_TRADING_URL = os.getenv(
    "ALPACA_TRADING_URL_PAPER",
    "https://paper-api.alpaca.markets/v2",
).rstrip("/")

EXPECTED_CONFIG_VERSION = "deltax_v2_strategy_v2"

DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_BATCH_SIZE = 35
DEFAULT_MAX_PAGES_PER_BATCH = 10
PAGE_LIMIT = 50
REQUEST_TIMEOUT_SECONDS = 30

CONFIRMATION_MINUTES = 10
NO_NEW_ENTRY_MINUTES = 30
INTRADAY_CLUSTER_MINUTES = 15

SOURCE = "alpaca_news"
UNIVERSE_FALLBACK = "alyrise_base"

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CompanyNewsEvent:
    external_id: str
    headline: str
    summary: str
    content: str
    source_url: str
    author: str
    published_at: datetime
    updated_at: datetime | None
    symbols: tuple[str, ...]
    raw_payload: dict[str, Any]

    @property
    def content_hash(self) -> str:
        material = "\n".join(
            [
                self.headline.strip().lower(),
                self.summary.strip().lower(),
                self.content.strip().lower(),
                self.source_url.strip().lower(),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def parse_iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    return parsed.astimezone(UTC)


def normalize_symbols(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()

    result = []
    for value in values:
        symbol = str(value or "").strip().upper()

        if (
            re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol)
            and symbol not in result
        ):
            result.append(symbol)

    return tuple(result)


def request_json(
    url: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str],
    provider: str,
) -> dict[str, Any]:
    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"{provider} request failed: {exc}"
        ) from exc

    if response.status_code in (401, 403):
        raise RuntimeError(
            f"{provider} rejected Alpaca credentials "
            f"(HTTP {response.status_code})"
        )

    if response.status_code == 429:
        raise RuntimeError(
            f"{provider} rate limit reached (HTTP 429)"
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"{provider} returned HTTP {response.status_code}"
        ) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"{provider} returned invalid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"{provider} returned unexpected payload type"
        )

    return payload


class CompanyNewsIngestion:
    def __init__(
        self,
        database_url: str = DATABASE_URL,
    ):
        self.database_url = database_url

    def validate_schema(self, cursor):
        required = {
            "strategy_configs": {
                "id", "version", "config", "is_active",
            },
            "universes": {"id"},
            "universe_memberships": {"universe_id"},
            "instruments": {"symbol"},
            "source_events": {
                "id",
                "source",
                "external_id",
                "source_type",
                "headline",
                "summary",
                "content",
                "source_url",
                "published_at",
                "source_updated_at",
                "content_hash",
                "processing_status",
                "raw_payload",
                "ingested_at",
            },
            "source_event_symbols": {
                "source_event_id",
                "symbol",
            },
            "event_clusters": {
                "id",
                "cluster_key",
                "primary_symbol",
                "event_type",
                "status",
                "first_published_at",
                "last_published_at",
                "updated_at",
            },
            "event_cluster_members": {
                "event_cluster_id",
                "source_event_id",
            },
        }

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

        for row in cursor.fetchall():
            actual.setdefault(
                row["table_name"],
                set(),
            ).add(row["column_name"])

        missing = {
            table: sorted(
                required_columns
                - actual.get(table, set())
            )
            for table, required_columns
            in required.items()
            if required_columns
            - actual.get(table, set())
        }

        if missing:
            raise RuntimeError(
                f"Database schema missing required columns: {missing}"
            )

    def active_config(self, cursor):
        cursor.execute(
            """
            SELECT id, version, config
            FROM strategy_configs
            WHERE is_active = true
            ORDER BY activated_at DESC NULLS LAST,
                     created_at DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()

        if row is None:
            raise RuntimeError(
                "No active strategy configuration found"
            )

        if row["version"] != EXPECTED_CONFIG_VERSION:
            raise RuntimeError(
                "Company-news ingestion requires active config "
                f"{EXPECTED_CONFIG_VERSION}, found {row['version']}"
            )

        return row

    def load_universe(self, cursor, config):
        config_json = config["config"] or {}
        universe_name = (
            config_json.get("universes", {}).get("base")
            or UNIVERSE_FALLBACK
        )

        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name IN (
                  'universes',
                  'universe_memberships'
              )
            """
        )

        columns: dict[str, set[str]] = {}

        for row in cursor.fetchall():
            columns.setdefault(
                row["table_name"],
                set(),
            ).add(row["column_name"])

        universe_name_columns = [
            name
            for name in (
                "universe_key",
                "name",
                "slug",
                "code",
            )
            if name in columns.get("universes", set())
        ]

        membership_symbol_column = next(
            (
                name
                for name in (
                    "symbol",
                    "instrument_symbol",
                )
                if name
                in columns.get(
                    "universe_memberships",
                    set(),
                )
            ),
            None,
        )

        if (
            not universe_name_columns
            or membership_symbol_column is None
        ):
            raise RuntimeError(
                "Could not identify universe DB columns"
            )

        symbols = []

        for universe_name_column in universe_name_columns:
            query = sql.SQL(
                """
                SELECT DISTINCT
                    memberships.{symbol_column} AS symbol
                FROM universe_memberships memberships
                JOIN universes universe_data
                  ON universe_data.id = memberships.universe_id
                JOIN instruments instrument_data
                  ON instrument_data.symbol =
                     memberships.{symbol_column}
                WHERE universe_data.{name_column} = %s
                ORDER BY symbol
                """
            ).format(
                symbol_column=sql.Identifier(
                    membership_symbol_column
                ),
                name_column=sql.Identifier(
                    universe_name_column
                ),
            )

            cursor.execute(
                query,
                (universe_name,),
            )

            symbols = [
                row["symbol"].upper()
                for row in cursor.fetchall()
            ]

            if symbols:
                break

        if not symbols:
            raise RuntimeError(
                f"Universe '{universe_name}' has no symbols"
            )

        return universe_name, symbols

    def detected_source_type(self, cursor):
        cursor.execute(
            """
            SELECT source_type, COUNT(*) AS count
            FROM source_events
            WHERE source = %s
            GROUP BY source_type
            ORDER BY count DESC
            LIMIT 1
            """,
            (SOURCE,),
        )

        row = cursor.fetchone()

        if row is None:
            raise RuntimeError(
                "No existing alpaca_news row exists to determine "
                "the schema-compatible source_type. "
                "Current DELTAX Neon already contains Alpaca news, "
                "so this should not occur."
            )

        return row["source_type"]

    def db_counts(self, cursor):
        cursor.execute(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM source_events
                    WHERE source = %s
                ) AS alpaca_source_events,
                (
                    SELECT COUNT(*)
                    FROM event_clusters
                    WHERE event_type = 'news'
                ) AS company_news_clusters,
                (
                    SELECT MAX(ingested_at)
                    FROM source_events
                    WHERE source = %s
                ) AS latest_alpaca_ingested_at
            """,
            (SOURCE, SOURCE),
        )

        return dict(cursor.fetchone())

    def health_check(self):
        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.validate_schema(cursor)
                config = self.active_config(cursor)
                universe_name, symbols = self.load_universe(
                    cursor,
                    config,
                )
                source_type = self.detected_source_type(
                    cursor
                )
                counts = self.db_counts(cursor)

        return {
            "config_version": config["version"],
            "universe": universe_name,
            "universe_size": len(symbols),
            "source": SOURCE,
            "detected_source_type": source_type,
            "alpaca_news_endpoint": ALPACA_NEWS_URL,
            "counts": counts,
            "rules": {
                "confirmation_minutes": CONFIRMATION_MINUTES,
                "intraday_cluster_minutes":
                    INTRADAY_CLUSTER_MINUTES,
                "no_new_entry_last_minutes":
                    NO_NEW_ENTRY_MINUTES,
            },
            "remote_requests_performed": 0,
            "database_writes_performed": False,
            "openai_requests_performed": 0,
            "broker_orders_submitted": False,
        }

    @staticmethod
    def chunks(values, size):
        for index in range(0, len(values), size):
            yield values[index:index + size]

    def fetch_batch(
        self,
        symbols,
        start,
        end,
        max_pages,
    ):
        headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY":
                ALPACA_API_SECRET,
            "Accept": "application/json",
            "User-Agent": "DELTAX-v2/1.0",
        }

        page_token = None
        events: dict[str, CompanyNewsEvent] = {}
        page_count = 0

        while page_count < max_pages:
            params: dict[str, Any] = {
                "symbols": ",".join(symbols),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "sort": "asc",
                "limit": PAGE_LIMIT,
                "include_content": "true",
            }

            if page_token:
                params["page_token"] = page_token

            payload = request_json(
                ALPACA_NEWS_URL,
                params=params,
                headers=headers,
                provider="Alpaca News API",
            )

            page_count += 1

            raw_news = payload.get("news")
            if not isinstance(raw_news, list):
                raise RuntimeError(
                    "Alpaca News API response is missing 'news' list"
                )

            for item in raw_news:
                if not isinstance(item, dict):
                    continue

                external_id = clean_text(
                    item.get("id")
                )
                headline = clean_text(
                    item.get("headline")
                )
                published_at = parse_iso_utc(
                    item.get("created_at")
                )

                if (
                    not external_id
                    or not headline
                    or published_at is None
                ):
                    continue

                event_symbols = tuple(
                    symbol
                    for symbol in normalize_symbols(
                        item.get("symbols")
                    )
                    if symbol in symbols
                )

                if not event_symbols:
                    continue

                events[external_id] = CompanyNewsEvent(
                    external_id=external_id,
                    headline=headline,
                    summary=clean_text(
                        item.get("summary")
                    ),
                    content=clean_text(
                        item.get("content")
                    ),
                    source_url=clean_text(
                        item.get("url")
                    ),
                    author=clean_text(
                        item.get("author")
                    ),
                    published_at=published_at,
                    updated_at=parse_iso_utc(
                        item.get("updated_at")
                    ),
                    symbols=event_symbols,
                    raw_payload=item,
                )

            page_token = clean_text(
                payload.get("next_page_token")
            )

            if not page_token:
                break

        return list(events.values()), page_count

    def fetch_all(
        self,
        symbols,
        lookback_hours,
        batch_size,
        max_pages_per_batch,
    ):
        now = datetime.now(UTC)
        start = now - timedelta(
            hours=lookback_hours
        )

        merged: dict[str, CompanyNewsEvent] = {}
        remote_requests = 0
        batches = 0

        for batch in self.chunks(
            symbols,
            batch_size,
        ):
            events, pages = self.fetch_batch(
                batch,
                start,
                now,
                max_pages_per_batch,
            )
            batches += 1
            remote_requests += pages

            for event in events:
                previous = merged.get(
                    event.external_id
                )

                if previous is None:
                    merged[
                        event.external_id
                    ] = event
                    continue

                combined_symbols = tuple(
                    sorted(
                        set(previous.symbols)
                        | set(event.symbols)
                    )
                )

                merged[event.external_id] = (
                    CompanyNewsEvent(
                        external_id=
                            event.external_id,
                        headline=event.headline,
                        summary=event.summary,
                        content=event.content,
                        source_url=
                            event.source_url,
                        author=event.author,
                        published_at=
                            event.published_at,
                        updated_at=
                            event.updated_at,
                        symbols=combined_symbols,
                        raw_payload=
                            event.raw_payload,
                    )
                )

        return (
            sorted(
                merged.values(),
                key=lambda item:
                    item.published_at,
            ),
            {
                "cutoff": start,
                "end": now,
                "batches": batches,
                "remote_requests":
                    remote_requests,
            },
        )

    def persist_events(
        self,
        cursor,
        events,
        source_type,
        valid_symbols,
    ):
        result = {
            "inserted": 0,
            "updated": 0,
            "symbol_links_inserted": 0,
        }

        for event in events:
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
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    'pending',
                    %s
                )
                ON CONFLICT (source, external_id)
                DO UPDATE SET
                    headline =
                        EXCLUDED.headline,
                    summary =
                        EXCLUDED.summary,
                    content =
                        EXCLUDED.content,
                    source_url =
                        EXCLUDED.source_url,
                    published_at =
                        EXCLUDED.published_at,
                    source_updated_at =
                        EXCLUDED.source_updated_at,
                    content_hash =
                        EXCLUDED.content_hash,
                    raw_payload =
                        EXCLUDED.raw_payload,
                    processing_status =
                        CASE
                            WHEN
                                source_events.content_hash
                                IS DISTINCT FROM
                                EXCLUDED.content_hash
                            THEN 'pending'
                            ELSE
                                source_events.processing_status
                        END
                RETURNING
                    id,
                    (xmax = 0) AS inserted
                """,
                (
                    SOURCE,
                    event.external_id,
                    source_type,
                    event.headline,
                    event.summary,
                    event.content,
                    event.source_url,
                    event.published_at,
                    event.updated_at,
                    event.content_hash,
                    Jsonb(
                        {
                            **event.raw_payload,
                            "deltax_ingestion":
                                "company_news_v1",
                            "author":
                                event.author,
                        }
                    ),
                ),
            )

            row = cursor.fetchone()
            event_id = row["id"]

            if row["inserted"]:
                result["inserted"] += 1
            else:
                result["updated"] += 1

            for symbol in event.symbols:
                if symbol not in valid_symbols:
                    continue

                cursor.execute(
                    """
                    INSERT INTO source_event_symbols (
                        source_event_id,
                        symbol
                    )
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        event_id,
                        symbol,
                    ),
                )

                result[
                    "symbol_links_inserted"
                ] += cursor.rowcount

        return result

    def load_stored_news(
        self,
        cursor,
        symbols,
        cutoff,
    ):
        cursor.execute(
            """
            SELECT
                events.id,
                links.symbol,
                events.external_id,
                events.headline,
                events.published_at
            FROM source_events events
            JOIN source_event_symbols links
              ON links.source_event_id =
                 events.id
            WHERE events.source = %s
              AND links.symbol = ANY(%s)
              AND events.published_at >= %s
            ORDER BY
                links.symbol,
                events.published_at,
                events.id
            """,
            (
                SOURCE,
                symbols,
                cutoff,
            ),
        )

        return [
            {
                "id": row["id"],
                "symbol":
                    row["symbol"].upper(),
                "external_id":
                    row["external_id"],
                "headline":
                    row["headline"] or "",
                "published_at":
                    row["published_at"].astimezone(
                        UTC
                    ),
            }
            for row in cursor.fetchall()
        ]

    def load_calendar(
        self,
        start_date,
        end_date,
    ):
        headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY":
                ALPACA_API_SECRET,
        }

        payload = request_json(
            f"{ALPACA_TRADING_URL}/calendar",
            params={
                "start":
                    start_date.isoformat(),
                "end":
                    end_date.isoformat(),
            },
            headers=headers,
            provider="Alpaca Calendar API",
        )

        # request_json expects dict, while calendar is a list.
        # Therefore this method is intentionally not used.
        return payload

    def fetch_calendar(
        self,
        start_date,
        end_date,
    ):
        try:
            response = requests.get(
                f"{ALPACA_TRADING_URL}/calendar",
                headers={
                    "APCA-API-KEY-ID":
                        ALPACA_API_KEY,
                    "APCA-API-SECRET-KEY":
                        ALPACA_API_SECRET,
                    "Accept":
                        "application/json",
                    "User-Agent":
                        "DELTAX-v2/1.0",
                },
                params={
                    "start":
                        start_date.isoformat(),
                    "end":
                        end_date.isoformat(),
                },
                timeout=
                    REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Alpaca calendar request failed: {exc}"
            ) from exc

        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, list):
            raise RuntimeError(
                "Alpaca calendar returned unexpected payload"
            )

        sessions = []

        for item in payload:
            session_date = datetime.strptime(
                item["date"],
                "%Y-%m-%d",
            ).date()

            open_time = time.fromisoformat(
                item["open"]
            )
            close_time = time.fromisoformat(
                item["close"]
            )

            sessions.append(
                {
                    "date": session_date,
                    "open": datetime.combine(
                        session_date,
                        open_time,
                        tzinfo=NEW_YORK,
                    ),
                    "close": datetime.combine(
                        session_date,
                        close_time,
                        tzinfo=NEW_YORK,
                    ),
                }
            )

        return sorted(
            sessions,
            key=lambda item: item["open"],
        )

    @staticmethod
    def ceil_minute(value):
        truncated = value.replace(
            second=0,
            microsecond=0,
        )

        if value == truncated:
            return value

        return truncated + timedelta(
            minutes=1
        )

    def resolve_anchor(
        self,
        published_at,
        sessions,
    ):
        published_et = published_at.astimezone(
            NEW_YORK
        )

        for session in sessions:
            market_open = session["open"]
            market_close = session["close"]

            if (
                market_open
                <= published_et
                < market_close
            ):
                candidate = self.ceil_minute(
                    published_et
                )

                confirmation_due = (
                    candidate
                    + timedelta(
                        minutes=
                            CONFIRMATION_MINUTES
                    )
                )

                latest_confirmation = (
                    market_close
                    - timedelta(
                        minutes=
                            NO_NEW_ENTRY_MINUTES
                    )
                )

                if (
                    confirmation_due
                    <= latest_confirmation
                ):
                    return {
                        "anchor":
                            candidate,
                        "confirmation_due":
                            confirmation_due,
                        "anchor_type":
                            "news_during_market",
                    }

                # News too late for a new entry today.
                # It may become actionable next regular session.
                continue

            if market_open > published_et:
                return {
                    "anchor":
                        market_open,
                    "confirmation_due":
                        market_open
                        + timedelta(
                            minutes=
                                CONFIRMATION_MINUTES
                        ),
                    "anchor_type":
                        "next_market_open",
                }

        return None

    def create_clusters(
        self,
        events,
        sessions,
    ):
        anchored = []

        for event in events:
            anchor = self.resolve_anchor(
                event["published_at"],
                sessions,
            )

            if anchor is None:
                continue

            anchored.append(
                {
                    **event,
                    **anchor,
                }
            )

        anchored.sort(
            key=lambda event: (
                event["symbol"],
                event["anchor"],
                event["published_at"],
            )
        )

        clusters = []

        for event in anchored:
            matching = None

            for cluster in reversed(
                clusters
            ):
                if (
                    cluster["symbol"]
                    != event["symbol"]
                ):
                    continue

                if (
                    event["anchor_type"]
                    == "next_market_open"
                    and
                    cluster["anchor_type"]
                    == "next_market_open"
                    and
                    event["anchor"]
                    == cluster["anchor"]
                ):
                    matching = cluster
                    break

                if (
                    event["anchor_type"]
                    == "news_during_market"
                    and
                    cluster["anchor_type"]
                    == "news_during_market"
                ):
                    limit_at = (
                        cluster["anchor"]
                        + timedelta(
                            minutes=
                                INTRADAY_CLUSTER_MINUTES
                        )
                    )

                    if (
                        event["anchor"]
                        <= limit_at
                    ):
                        matching = cluster
                        break

                break

            if matching is None:
                clusters.append(
                    {
                        "symbol":
                            event["symbol"],
                        "anchor":
                            event["anchor"],
                        "confirmation_due":
                            event[
                                "confirmation_due"
                            ],
                        "anchor_type":
                            event[
                                "anchor_type"
                            ],
                        "events":
                            [event],
                    }
                )
            else:
                matching[
                    "events"
                ].append(event)

        return clusters

    @staticmethod
    def cluster_key(cluster):
        anchor_utc = cluster[
            "anchor"
        ].astimezone(UTC)

        return (
            "deltax_news_v1:"
            f"{cluster['symbol']}:"
            f"{cluster['anchor_type']}:"
            f"{anchor_utc.strftime('%Y%m%dT%H%M%SZ')}"
        )

    def persist_clusters(
        self,
        cursor,
        clusters,
        now,
    ):
        result = {
            "created_clusters": 0,
            "updated_clusters": 0,
            "member_links_inserted": 0,
        }

        for cluster in clusters:
            key = self.cluster_key(cluster)

            first_published = min(
                event["published_at"]
                for event in cluster["events"]
            )
            last_published = max(
                event["published_at"]
                for event in cluster["events"]
            )

            status = (
                "open"
                if cluster[
                    "confirmation_due"
                ].astimezone(UTC) > now
                else "closed"
            )

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
                VALUES (
                    %s, %s, 'news',
                    %s, %s, %s
                )
                ON CONFLICT (cluster_key)
                DO UPDATE SET
                    primary_symbol =
                        EXCLUDED.primary_symbol,
                    event_type = 'news',
                    status =
                        EXCLUDED.status,
                    first_published_at =
                        LEAST(
                            event_clusters.first_published_at,
                            EXCLUDED.first_published_at
                        ),
                    last_published_at =
                        GREATEST(
                            event_clusters.last_published_at,
                            EXCLUDED.last_published_at
                        ),
                    updated_at = now()
                RETURNING
                    id,
                    (xmax = 0) AS inserted
                """,
                (
                    key,
                    cluster["symbol"],
                    status,
                    first_published,
                    last_published,
                ),
            )

            row = cursor.fetchone()
            cluster_id = row["id"]

            if row["inserted"]:
                result[
                    "created_clusters"
                ] += 1
            else:
                result[
                    "updated_clusters"
                ] += 1

            for event in cluster["events"]:
                cursor.execute(
                    """
                    INSERT INTO event_cluster_members (
                        event_cluster_id,
                        source_event_id
                    )
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        cluster_id,
                        event["id"],
                    ),
                )

                result[
                    "member_links_inserted"
                ] += cursor.rowcount

        return result

    def run(
        self,
        *,
        apply,
        lookback_hours,
        batch_size,
        max_pages_per_batch,
    ):
        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                self.validate_schema(cursor)
                config = self.active_config(
                    cursor
                )
                universe_name, symbols = (
                    self.load_universe(
                        cursor,
                        config,
                    )
                )
                source_type = (
                    self.detected_source_type(
                        cursor
                    )
                )

            events, fetch_meta = self.fetch_all(
                symbols,
                lookback_hours,
                batch_size,
                max_pages_per_batch,
            )

            result = {
                "mode":
                    "apply"
                    if apply
                    else "dry_run",
                "config_version":
                    config["version"],
                "universe":
                    universe_name,
                "universe_size":
                    len(symbols),
                "source":
                    SOURCE,
                "source_type":
                    source_type,
                "lookback_hours":
                    lookback_hours,
                "fetched_unique_articles":
                    len(events),
                "fetch":
                    fetch_meta,
                "sample": [
                    {
                        "external_id":
                            event.external_id,
                        "published_at":
                            event.published_at,
                        "symbols":
                            list(
                                event.symbols
                            ),
                        "headline":
                            event.headline,
                    }
                    for event
                    in events[-5:]
                ],
                "openai_requests_performed":
                    0,
                "broker_orders_submitted":
                    False,
            }

            if not apply:
                connection.rollback()
                result[
                    "database_writes_performed"
                ] = False
                return result

            valid_symbols = set(symbols)

            with connection.cursor() as cursor:
                event_result = (
                    self.persist_events(
                        cursor,
                        events,
                        source_type,
                        valid_symbols,
                    )
                )

            # Cluster all stored Alpaca company news in the same
            # lookback window, not merely articles returned in this
            # request. This keeps clustering stable across pagination.
            cutoff = (
                datetime.now(UTC)
                - timedelta(
                    hours=
                        lookback_hours
                )
            )

            with connection.cursor() as cursor:
                stored_news = (
                    self.load_stored_news(
                        cursor,
                        symbols,
                        cutoff,
                    )
                )

            if stored_news:
                oldest = min(
                    event["published_at"]
                    for event in stored_news
                )
                newest = max(
                    event["published_at"]
                    for event in stored_news
                )

                sessions = self.fetch_calendar(
                    oldest.date()
                    - timedelta(days=3),
                    newest.date()
                    + timedelta(days=10),
                )

                clusters = self.create_clusters(
                    stored_news,
                    sessions,
                )
            else:
                clusters = []

            with connection.cursor() as cursor:
                cluster_result = (
                    self.persist_clusters(
                        cursor,
                        clusters,
                        datetime.now(UTC),
                    )
                )

            connection.commit()

            result.update(
                {
                    "stored_news_in_window":
                        len(stored_news),
                    "calculated_clusters":
                        len(clusters),
                    "event_persistence":
                        event_result,
                    "cluster_persistence":
                        cluster_result,
                    "database_writes_performed":
                        True,
                }
            )

            return result


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "DELTAX production Alpaca company-news "
            "ingestion and clustering."
        )
    )

    mode = parser.add_mutually_exclusive_group(
        required=True
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "Validate DB/config only. "
            "No API calls and no writes."
        ),
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Fetch Alpaca company news but do not write."
        ),
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Fetch, persist and cluster Alpaca company news."
        ),
    )

    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
    )
    parser.add_argument(
        "--max-pages-per-batch",
        type=int,
        default=DEFAULT_MAX_PAGES_PER_BATCH,
    )

    args = parser.parse_args()

    if not 1 <= args.lookback_hours <= 168:
        parser.error(
            "--lookback-hours must be between 1 and 168"
        )

    if not 1 <= args.batch_size <= 50:
        parser.error(
            "--batch-size must be between 1 and 50"
        )

    if not 1 <= args.max_pages_per_batch <= 50:
        parser.error(
            "--max-pages-per-batch must be between 1 and 50"
        )

    return args


def main():
    args = parse_args()
    ingestion = CompanyNewsIngestion()

    if args.check:
        result = ingestion.health_check()
        print(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        print(
            "COMPANY NEWS INGESTION HEALTH CHECK: OK"
        )
        return

    result = ingestion.run(
        apply=args.apply,
        lookback_hours=
            args.lookback_hours,
        batch_size=
            args.batch_size,
        max_pages_per_batch=
            args.max_pages_per_batch,
    )

    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )

    print(
        "COMPANY NEWS INGESTION: OK"
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
