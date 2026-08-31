# File: helpers/test_gdeltdoc_market_news.py
# Purpose: Tests GDELT DOC 2.0 through the gdeltdoc Python client without database or AI writes.

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

import requests
from gdeltdoc import Filters, GdeltDoc
import gdeltdoc.api_client as gdelt_api_client


KEYWORDS = ["Iranian", "Hormuz"]
TIMESPAN = "1h"
MAX_RECORDS = 10
REQUEST_TIMEOUT_SECONDS = 45


def bounded_get(*args: Any, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
    return ORIGINAL_REQUESTS_GET(*args, **kwargs)


ORIGINAL_REQUESTS_GET = requests.get
gdelt_api_client.requests.get = bounded_get


def parse_seen_time(value: Any) -> datetime | None:
    if value is None:
        return None

    cleaned = str(value).strip()
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(cleaned, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def main() -> int:
    print("GDELT DOC PYTHON LIBRARY TEST")
    print(f"Library: gdeltdoc")
    print(f"Keywords: {', '.join(KEYWORDS)}")
    print(f"Timespan: {TIMESPAN}")
    print(f"Maximum records: {MAX_RECORDS}")

    filters = Filters(
        keyword=KEYWORDS,
        timespan=TIMESPAN,
        num_records=MAX_RECORDS,
    )
    client = GdeltDoc()

    try:
        articles = client.article_search(filters)
    except requests.RequestException as exc:
        print(f"ERROR: gdeltdoc request failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"ERROR: gdeltdoc failed with {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"Articles returned: {len(articles.index)}")
    if articles.empty:
        print("No matching articles were returned.")
    else:
        now = datetime.now(timezone.utc)
        for index, (_, article) in enumerate(articles.iterrows(), start=1):
            seen_at = parse_seen_time(article.get("seendate"))
            age = (
                max(0.0, (now - seen_at).total_seconds() / 60.0)
                if seen_at is not None
                else None
            )
            age_text = f"{age:.1f} min" if age is not None else "unknown"

            print("-" * 78)
            print(f"Article {index}")
            print(
                f"Seen UTC: {seen_at.isoformat() if seen_at else 'unknown'} "
                f"| age={age_text}"
            )
            print(f"Domain: {article.get('domain') or 'n/a'}")
            print(f"Source country: {article.get('sourcecountry') or 'n/a'}")
            print(f"Language: {article.get('language') or 'n/a'}")
            print(f"Title: {article.get('title') or 'n/a'}")

    print("\nNo API key was required.")
    print("No database writes were performed.")
    print("No OpenAI requests were performed.")
    print("GDELT DOC PYTHON LIBRARY TEST: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
