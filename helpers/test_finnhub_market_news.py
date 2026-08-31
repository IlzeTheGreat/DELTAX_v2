# File: helpers/test_finnhub_market_news.py
# Purpose: Verifies free-plan Finnhub general market-news access, freshness, and geopolitical coverage without exposing the API key.

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


FINNHUB_MARKET_NEWS_URL = "https://finnhub.io/api/v1/news"
GEOPOLITICAL_KEYWORDS = (
    "iran",
    "hormuz",
    "strike",
    "bomb",
    "missile",
    "oil",
    "war",
    "military",
    "sanction",
)
MAX_LATEST_ITEMS = 15
MAX_MATCHES = 20


def utc_from_epoch(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def age_minutes(published_at: datetime | None, now: datetime) -> float | None:
    if published_at is None:
        return None
    return max(0.0, (now - published_at).total_seconds() / 60.0)


def format_time(value: datetime | None) -> str:
    return value.isoformat() if value else "unknown"


def format_age(value: float | None) -> str:
    return f"{value:.1f} min" if value is not None else "unknown"


def fetch_general_news(api_key: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            FINNHUB_MARKET_NEWS_URL,
            params={"category": "general", "minId": 0, "token": api_key},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Finnhub request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise RuntimeError(
            f"Finnhub returned HTTP {response.status_code}. Check FINNHUB_API_KEY "
            "and whether the endpoint is available for the account plan."
        )
    if response.status_code == 429:
        raise RuntimeError(
            "Finnhub returned HTTP 429: rate limit reached. Wait briefly and run the test again."
        )

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"Finnhub returned HTTP {response.status_code}.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Finnhub returned a response that is not valid JSON.") from exc

    if not isinstance(payload, list):
        raise RuntimeError(
            f"Unexpected Finnhub response type: {type(payload).__name__}; expected a list."
        )
    if not payload:
        raise RuntimeError("Finnhub returned an empty general market-news list.")

    return [item for item in payload if isinstance(item, dict)]


def print_item(item: dict[str, Any], now: datetime) -> None:
    published_at = utc_from_epoch(item.get("datetime"))
    item_age = age_minutes(published_at, now)
    print(f"ID: {item.get('id', 'n/a')}")
    print(f"Published UTC: {format_time(published_at)} | age={format_age(item_age)}")
    print(f"Source: {item.get('source') or 'n/a'}")
    print(f"Headline: {item.get('headline') or 'n/a'}")


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not api_key:
        print("ERROR: FINNHUB_API_KEY is missing from the project .env file.", file=sys.stderr)
        return 1

    try:
        news = fetch_general_news(api_key)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    news.sort(key=lambda item: int(item.get("datetime") or 0), reverse=True)
    now = datetime.now(timezone.utc)
    latest_time = utc_from_epoch(news[0].get("datetime"))
    latest_age = age_minutes(latest_time, now)

    if latest_age is None:
        freshness = "unknown"
    elif latest_age <= 15:
        freshness = "excellent"
    elif latest_age <= 60:
        freshness = "acceptable"
    else:
        freshness = "stale"

    print("FINNHUB GENERAL MARKET NEWS TEST")
    print(f"Articles returned: {len(news)}")
    print(f"Latest article UTC: {format_time(latest_time)}")
    print(f"Latest article age: {format_age(latest_age)}")
    print(f"Freshness: {freshness}")

    print("\nLATEST ARTICLES")
    for index, item in enumerate(news[:MAX_LATEST_ITEMS], start=1):
        print("-" * 78)
        print(f"Article {index}")
        print_item(item, now)

    matches: list[tuple[dict[str, Any], list[str]]] = []
    for item in news:
        searchable = f"{item.get('headline') or ''} {item.get('summary') or ''}".lower()
        matched = [keyword for keyword in GEOPOLITICAL_KEYWORDS if keyword in searchable]
        if matched:
            matches.append((item, matched))

    print("\nGEOPOLITICAL / ENERGY KEYWORD MATCHES")
    print(f"Matches found: {len(matches)}")
    for index, (item, matched) in enumerate(matches[:MAX_MATCHES], start=1):
        print("-" * 78)
        print(f"Match {index} | keywords={', '.join(matched)}")
        print_item(item, now)

    if not matches:
        print("No configured geopolitical or energy keywords were found in this response.")

    print("\nNo database writes were performed.")
    print("No OpenAI requests were performed.")
    print("The FINNHUB_API_KEY value was not printed.")
    print("FINNHUB MARKET NEWS TEST: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
