# File: helpers/test_news_event_clusters.py
# Purpose: Grupē saglabātās ziņas vienā tirdzniecības notikumā, lai dublētas ziņas neradītu vairākus signālus.

import os
import sys
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import psycopg
import requests
from dotenv import load_dotenv


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
ALPACA_API_KEY = os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_API_SECRET = os.environ["ALPACA_API_SECRET_PAPER"]
ALPACA_TRADING_URL = os.getenv(
    "ALPACA_TRADING_URL_PAPER",
    "https://paper-api.alpaca.markets/v2",
).rstrip("/")

NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

CONFIRMATION_MINUTES = 10
NO_NEW_ENTRY_MINUTES = 30
INTRADAY_CLUSTER_MINUTES = 15

DEFAULT_SYMBOLS = ["IREN", "PCG", "RKLB", "CW"]


def ceil_minute(value):
    value = value.replace(second=0, microsecond=0)
    return value + timedelta(minutes=1)


def load_news(symbols):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    se.id,
                    ses.symbol,
                    se.external_id,
                    se.headline,
                    se.published_at
                FROM source_events se
                JOIN source_event_symbols ses
                    ON ses.source_event_id = se.id
                WHERE se.source = 'alpaca_news'
                  AND ses.symbol = ANY(%s)
                ORDER BY se.published_at, ses.symbol
                """,
                (symbols,),
            )

            return [
                {
                    "id": row[0],
                    "symbol": row[1],
                    "external_id": row[2],
                    "headline": row[3],
                    "published_at": row[4].astimezone(UTC),
                }
                for row in cursor.fetchall()
            ]


def load_calendar(start_date, end_date):
    response = requests.get(
        f"{ALPACA_TRADING_URL}/calendar",
        headers={
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_API_SECRET,
        },
        params={
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        },
        timeout=30,
    )
    response.raise_for_status()

    sessions = []

    for item in response.json():
        session_date = datetime.strptime(item["date"], "%Y-%m-%d").date()
        open_time = time.fromisoformat(item["open"])
        close_time = time.fromisoformat(item["close"])

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

    return sorted(sessions, key=lambda session: session["open"])


def resolve_anchor(published_at, sessions):
    published_et = published_at.astimezone(NEW_YORK)

    for session in sessions:
        market_open = session["open"]
        market_close = session["close"]

        if market_open <= published_et < market_close:
            candidate = ceil_minute(published_et)

            latest_allowed_confirmation = (
                market_close - timedelta(minutes=NO_NEW_ENTRY_MINUTES)
            )

            confirmation_due = candidate + timedelta(
                minutes=CONFIRMATION_MINUTES
            )

            if confirmation_due <= latest_allowed_confirmation:
                return {
                    "anchor": candidate,
                    "confirmation_due": confirmation_due,
                    "anchor_type": "news_during_market",
                }

        if market_open > published_et:
            return {
                "anchor": market_open,
                "confirmation_due": market_open
                + timedelta(minutes=CONFIRMATION_MINUTES),
                "anchor_type": "next_market_open",
            }

    return None


def create_clusters(events):
    clusters = []

    events = sorted(
        events,
        key=lambda event: (
            event["symbol"],
            event["anchor"],
            event["published_at"],
        ),
    )

    for event in events:
        matching_cluster = None

        for cluster in reversed(clusters):
            if cluster["symbol"] != event["symbol"]:
                continue

            if (
                event["anchor_type"] == "next_market_open"
                and cluster["anchor_type"] == "next_market_open"
                and event["anchor"] == cluster["anchor"]
            ):
                matching_cluster = cluster
                break

            if (
                event["anchor_type"] == "news_during_market"
                and cluster["anchor_type"] == "news_during_market"
            ):
                cluster_limit = cluster["anchor"] + timedelta(
                    minutes=INTRADAY_CLUSTER_MINUTES
                )

                if event["anchor"] <= cluster_limit:
                    matching_cluster = cluster
                    break

            break

        if matching_cluster is None:
            clusters.append(
                {
                    "symbol": event["symbol"],
                    "anchor": event["anchor"],
                    "confirmation_due": event["confirmation_due"],
                    "anchor_type": event["anchor_type"],
                    "events": [event],
                }
            )
        else:
            matching_cluster["events"].append(event)

    return clusters


def main():
    symbols = [symbol.upper() for symbol in sys.argv[1:]]
    if not symbols:
        symbols = DEFAULT_SYMBOLS

    news = load_news(symbols)

    if not news:
        raise RuntimeError("No stored Alpaca news found")

    print(f"Stored news events: {len(news)}")
    print(f"Symbols: {', '.join(symbols)}")

    oldest = min(event["published_at"] for event in news)
    newest = max(event["published_at"] for event in news)

    sessions = load_calendar(
        oldest.date() - timedelta(days=3),
        newest.date() + timedelta(days=10),
    )

    anchored_events = []
    unresolved_events = []

    for event in news:
        anchor = resolve_anchor(event["published_at"], sessions)

        if anchor is None:
            unresolved_events.append(event)
            continue

        anchored_events.append({**event, **anchor})

    clusters = create_clusters(anchored_events)

    duplicate_events = sum(
        max(0, len(cluster["events"]) - 1)
        for cluster in clusters
    )

    print("\nCLUSTER SUMMARY")
    print(f"Anchored events: {len(anchored_events)}")
    print(f"Event clusters: {len(clusters)}")
    print(f"Duplicate signals prevented: {duplicate_events}")
    print(f"Unresolved events: {len(unresolved_events)}")

    print("\nEVENT CLUSTERS")

    for number, cluster in enumerate(
        sorted(clusters, key=lambda item: item["anchor"], reverse=True),
        start=1,
    ):
        print(
            f"\n{number}. {cluster['symbol']} | "
            f"anchor={cluster['anchor']} | "
            f"type={cluster['anchor_type']} | "
            f"articles={len(cluster['events'])}"
        )
        print(f"confirmation_due={cluster['confirmation_due']}")

        for event in cluster["events"]:
            print(
                f"  {event['published_at']} | "
                f"{event['external_id']} | "
                f"{event['headline']}"
            )

    if unresolved_events:
        print("\nUNRESOLVED EVENTS")

        for event in unresolved_events:
            print(
                f"{event['symbol']} | "
                f"{event['published_at']} | "
                f"{event['headline']}"
            )

    assert anchored_events, "No news events received anchors"
    assert clusters, "No event clusters created"

    print("\nNEWS EVENT CLUSTER TEST: OK")


if __name__ == "__main__":
    main()