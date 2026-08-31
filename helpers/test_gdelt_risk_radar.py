# File: helpers/test_gdelt_risk_radar.py
# Purpose: Tests four portfolio-relevant GDELT risk searches without database or AI writes.

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests
from gdeltdoc import Filters, GdeltDoc
import gdeltdoc.api_client as gdelt_api_client


TIMESPAN = "1h"
MAX_RECORDS_PER_GROUP = 25
MAX_PRINTED_PER_GROUP = 10
REQUEST_TIMEOUT_SECONDS = 45
PAUSE_BETWEEN_GROUPS_SECONDS = 3

RISK_GROUPS = {
    "conflict_and_trade": {
        "keywords": [
            "airstrike",
            "missile attack",
            "military invasion",
            "economic sanctions",
            "export controls",
            "trade tariffs",
            "Taiwan Strait",
            "Strait of Hormuz",
        ],
        "exposures": "technology, semiconductors, energy, defense, industrials, consumer",
    },
    "energy_and_supply_chain": {
        "keywords": [
            "oil supply disruption",
            "natural gas disruption",
            "OPEC production",
            "pipeline outage",
            "refinery outage",
            "port closure",
            "shipping disruption",
            "semiconductor shortage",
            "aircraft grounding",
        ],
        "exposures": "energy, utilities, semiconductors, aerospace, industrials, retail",
    },
    "financial_system_and_policy": {
        "keywords": [
            "bank failure",
            "banking crisis",
            "sovereign default",
            "credit downgrade",
            "liquidity crisis",
            "antitrust ruling",
            "drug pricing",
            "Medicare payment",
            "defense budget",
        ],
        "exposures": "financials, real estate, technology, healthcare, defense",
    },
    "cyber_infrastructure_and_disasters": {
        "keywords": [
            "cyberattack",
            "ransomware attack",
            "cloud outage",
            "power grid failure",
            "nuclear plant outage",
            "earthquake",
            "hurricane",
            "wildfire",
        ],
        "exposures": "technology, utilities, energy, industrials, consumer, insurance",
    },
}

TRUSTED_DOMAIN_SUFFIXES = (
    "reuters.com",
    "apnews.com",
    "bloomberg.com",
    "bbc.com",
    "bbc.co.uk",
    "cnbc.com",
    "ft.com",
    "wsj.com",
    "federalreserve.gov",
    "treasury.gov",
    "sec.gov",
    "fda.gov",
    "defense.gov",
    "whitehouse.gov",
)


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


def normalized_title(value: Any) -> str:
    text = str(value or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def article_key(article: dict[str, Any]) -> str:
    title_key = normalized_title(article.get("title"))
    if title_key:
        return f"title:{title_key}"
    return f"url:{str(article.get('url') or '').strip().lower()}"


def is_trusted_domain(value: Any) -> bool:
    domain = str(value or "").strip().lower().removeprefix("www.")
    return any(
        domain == suffix or domain.endswith(f".{suffix}")
        for suffix in TRUSTED_DOMAIN_SUFFIXES
    )


def dataframe_records(dataframe: Any) -> list[dict[str, Any]]:
    if dataframe.empty:
        return []
    return [dict(record) for record in dataframe.to_dict(orient="records")]


def fetch_group(client: GdeltDoc, keywords: list[str]) -> list[dict[str, Any]]:
    filters = Filters(
        keyword=keywords,
        timespan=TIMESPAN,
        num_records=MAX_RECORDS_PER_GROUP,
    )
    return dataframe_records(client.article_search(filters))


def main() -> int:
    print("GDELT PORTFOLIO RISK RADAR TEST")
    print(f"Risk groups: {len(RISK_GROUPS)}")
    print(f"Timespan per group: {TIMESPAN}")
    print(f"Maximum records per group: {MAX_RECORDS_PER_GROUP}")
    print("AI requests: no")
    print("Database writes: no")

    client = GdeltDoc()
    all_unique: dict[str, dict[str, Any]] = {}
    group_summaries: list[dict[str, Any]] = []
    failures: list[str] = []
    now = datetime.now(timezone.utc)

    for group_number, (group_name, config) in enumerate(RISK_GROUPS.items(), start=1):
        if group_number > 1:
            time.sleep(PAUSE_BETWEEN_GROUPS_SECONDS)

        print("\n" + "=" * 78)
        print(f"GROUP: {group_name}")
        print(f"Keywords: {', '.join(config['keywords'])}")
        print(f"Portfolio exposures: {config['exposures']}")

        try:
            raw_articles = fetch_group(client, config["keywords"])
        except requests.RequestException as exc:
            failures.append(group_name)
            print(f"ERROR: request failed: {exc}")
            continue
        except Exception as exc:
            failures.append(group_name)
            print(f"ERROR: {type(exc).__name__}: {exc}")
            continue

        unique_in_group: dict[str, dict[str, Any]] = {}
        for article in raw_articles:
            unique_in_group.setdefault(article_key(article), article)
            all_unique.setdefault(article_key(article), article)

        ordered = sorted(
            unique_in_group.values(),
            key=lambda article: parse_seen_time(article.get("seendate"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        trusted_count = sum(is_trusted_domain(item.get("domain")) for item in ordered)
        latest_seen = parse_seen_time(ordered[0].get("seendate")) if ordered else None
        latest_age = (
            max(0.0, (now - latest_seen).total_seconds() / 60.0)
            if latest_seen is not None
            else None
        )

        print(f"Raw articles: {len(raw_articles)}")
        print(f"Unique articles: {len(ordered)}")
        print(f"Trusted-domain articles: {trusted_count}")
        print(
            "Latest seen age: "
            + (f"{latest_age:.1f} min" if latest_age is not None else "unknown")
        )

        group_summaries.append(
            {
                "group": group_name,
                "raw": len(raw_articles),
                "unique": len(ordered),
                "trusted": trusted_count,
                "latest_age": latest_age,
            }
        )

        for index, article in enumerate(ordered[:MAX_PRINTED_PER_GROUP], start=1):
            seen_at = parse_seen_time(article.get("seendate"))
            seen_age = (
                max(0.0, (now - seen_at).total_seconds() / 60.0)
                if seen_at is not None
                else None
            )
            print("-" * 78)
            print(f"Article {index}")
            print(
                f"Seen UTC: {seen_at.isoformat() if seen_at else 'unknown'} "
                f"| age={f'{seen_age:.1f} min' if seen_age is not None else 'unknown'}"
            )
            print(
                f"Domain: {article.get('domain') or 'n/a'} "
                f"| trusted={is_trusted_domain(article.get('domain'))}"
            )
            print(f"Language: {article.get('language') or 'n/a'}")
            print(f"Title: {article.get('title') or 'n/a'}")

    print("\n" + "=" * 78)
    print("SUMMARY")
    for summary in group_summaries:
        age_text = (
            f"{summary['latest_age']:.1f} min"
            if summary["latest_age"] is not None
            else "unknown"
        )
        print(
            f"- {summary['group']}: raw={summary['raw']} | "
            f"unique={summary['unique']} | trusted={summary['trusted']} | "
            f"latest_age={age_text}"
        )

    print(f"Unique articles across all groups: {len(all_unique)}")
    print(f"Failed groups: {len(failures)}")
    if failures:
        print(f"Failure list: {', '.join(failures)}")
        print("GDELT PORTFOLIO RISK RADAR TEST: PARTIAL")
        return 1

    print("No database writes were performed.")
    print("No OpenAI requests were performed.")
    print("GDELT PORTFOLIO RISK RADAR TEST: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
