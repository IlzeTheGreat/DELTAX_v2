# File: helpers/test_marketaux_market_news.py
# Purpose: Verifies Marketaux free-plan market-news access, quota headers, freshness, and entity metadata without database or AI writes.

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


MARKETAUX_NEWS_URL = "https://api.marketaux.com/v1/news/all"
REQUEST_TIMEOUT_SECONDS = 30
FREE_PLAN_LIMIT = 3
LOOKBACK_HOURS = 12
MARKET_RISK_SEARCH = (
    '(airstrike|"missile attack"|"economic sanctions"|"export controls"|tariff|'
    '"Strait of Hormuz"|"Taiwan Strait"|"oil supply"|OPEC|"pipeline outage"|'
    '"refinery outage"|"shipping disruption"|"semiconductor shortage"|'
    '"aircraft grounding"|"bank failure"|"banking crisis"|"credit downgrade"|'
    'cyberattack|ransomware|"cloud outage"|"power grid"|"nuclear plant"|'
    'earthquake|hurricane|wildfire|"drug pricing"|Medicare|antitrust)'
)


def parse_utc_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_minutes(published_at: datetime | None, now: datetime) -> float | None:
    if published_at is None:
        return None
    return max(0.0, (now - published_at).total_seconds() / 60.0)


def response_header(response: requests.Response, name: str) -> str:
    return response.headers.get(name, "not provided")


def extract_symbols(article: dict[str, Any]) -> list[str]:
    entities = article.get("entities")
    if not isinstance(entities, list):
        return []

    symbols: list[str] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        symbol = str(entity.get("symbol") or "").strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    api_token = os.getenv("MARKETAUX_API_TOKEN", "").strip()
    if not api_token:
        print("ERROR: MARKETAUX_API_TOKEN is missing from the project .env file.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    published_after = now - timedelta(hours=LOOKBACK_HOURS)

    try:
        response = requests.get(
            MARKETAUX_NEWS_URL,
            params={
                "api_token": api_token,
                "language": "en",
                "group_similar": "true",
                "search": MARKET_RISK_SEARCH,
                "published_after": published_after.strftime("%Y-%m-%dT%H:%M:%S"),
                "sort": "published_at",
                "limit": FREE_PLAN_LIMIT,
            },
            headers={
                "Accept": "application/json",
                "User-Agent": "DELTAX-v2-news-monitor/1.0",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        print(f"ERROR: Marketaux request failed: {exc}", file=sys.stderr)
        return 1

    if response.status_code == 401:
        print("ERROR: Marketaux rejected the API token (HTTP 401).", file=sys.stderr)
        return 1
    if response.status_code == 402:
        print("ERROR: Marketaux daily usage limit has been reached (HTTP 402).", file=sys.stderr)
        return 1
    if response.status_code == 429:
        print("ERROR: Marketaux per-minute rate limit has been reached (HTTP 429).", file=sys.stderr)
        return 1

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        print(f"ERROR: Marketaux returned HTTP {response.status_code}.", file=sys.stderr)
        return 1

    try:
        payload = response.json()
    except ValueError:
        print("ERROR: Marketaux returned a response that is not valid JSON.", file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print("ERROR: Unexpected Marketaux response type.", file=sys.stderr)
        return 1

    meta = payload.get("meta")
    articles = payload.get("data")
    if not isinstance(meta, dict) or not isinstance(articles, list):
        print("ERROR: Marketaux response is missing meta or data.", file=sys.stderr)
        return 1

    articles = [item for item in articles if isinstance(item, dict)]
    articles.sort(
        key=lambda item: parse_utc_time(item.get("published_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    print("MARKETAUX MARKET NEWS TEST")
    print("Mode: portfolio-relevant market-risk search")
    print(f"Query lookback: {LOOKBACK_HOURS} hours")
    print(f"Search query: {MARKET_RISK_SEARCH}")
    print(f"Meta found: {meta.get('found', 'n/a')}")
    print(f"Meta returned: {meta.get('returned', len(articles))}")
    print(f"Meta limit: {meta.get('limit', 'n/a')}")
    print(f"Usage limit: {response_header(response, 'X-UsageLimit-Limit')}")
    print(f"Usage remaining: {response_header(response, 'X-UsageLimit-Remaining')}")
    print(f"Minute rate limit: {response_header(response, 'X-RateLimit-Limit')}")
    print(f"Minute rate remaining: {response_header(response, 'X-RateLimit-Remaining')}")

    if not articles:
        print("No English-language articles were returned for the selected lookback.")

    for index, article in enumerate(articles, start=1):
        published_at = parse_utc_time(article.get("published_at"))
        article_age = age_minutes(published_at, now)
        symbols = extract_symbols(article)
        snippet = " ".join(str(article.get("snippet") or "").split())
        if len(snippet) > 300:
            snippet = snippet[:297] + "..."

        print("-" * 78)
        print(f"Article {index}")
        print(
            f"Published UTC: {published_at.isoformat() if published_at else 'unknown'} "
            f"| age={f'{article_age:.1f} min' if article_age is not None else 'unknown'}"
        )
        print(f"Source: {article.get('source') or 'n/a'}")
        print(f"Title: {article.get('title') or 'n/a'}")
        print(f"Symbols: {', '.join(symbols) if symbols else 'none'}")
        print(f"Snippet: {snippet or 'n/a'}")

    print("\nOnly one Marketaux API request was performed.")
    print("No database writes were performed.")
    print("No OpenAI requests were performed.")
    print("The MARKETAUX_API_TOKEN value was not printed.")
    print("MARKETAUX MARKET NEWS TEST: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
