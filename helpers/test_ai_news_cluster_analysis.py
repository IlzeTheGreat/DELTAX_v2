# File: helpers/test_ai_news_cluster_analysis.py
# Purpose: Analyzes the latest news cluster for each symbol and applies deterministic stock and options news gates.

import json
import os
import sys
from datetime import timedelta

import psycopg
from dotenv import load_dotenv
from openai import OpenAI

from test_news_event_clusters import (
    create_clusters,
    load_calendar,
    resolve_anchor,
)


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

DEFAULT_SYMBOLS = ["IREN", "PCG", "RKLB", "CW"]

client = OpenAI(api_key=OPENAI_API_KEY)


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
                    COALESCE(se.summary, ''),
                    COALESCE(se.content, ''),
                    se.published_at
                FROM source_events se
                JOIN source_event_symbols ses
                    ON ses.source_event_id = se.id
                WHERE se.source = 'alpaca_news'
                  AND ses.symbol = ANY(%s)
                ORDER BY se.published_at
                """,
                (symbols,),
            )

            return [
                {
                    "id": row[0],
                    "symbol": row[1],
                    "external_id": row[2],
                    "headline": row[3],
                    "summary": row[4],
                    "content": row[5],
                    "published_at": row[6],
                }
                for row in cursor.fetchall()
            ]


def build_clusters(news):
    oldest = min(event["published_at"] for event in news)
    newest = max(event["published_at"] for event in news)

    sessions = load_calendar(
        oldest.date() - timedelta(days=3),
        newest.date() + timedelta(days=10),
    )

    anchored_events = []

    for event in news:
        anchor = resolve_anchor(event["published_at"], sessions)

        if anchor is not None:
            anchored_events.append({**event, **anchor})

    return create_clusters(anchored_events)


def select_latest_cluster_per_symbol(clusters):
    latest = {}

    for cluster in clusters:
        symbol = cluster["symbol"]

        if (
            symbol not in latest
            or cluster["anchor"] > latest[symbol]["anchor"]
        ):
            latest[symbol] = cluster

    return latest


def prepare_cluster_text(cluster):
    articles = []

    for event in cluster["events"]:
        summary = event["summary"].strip()
        content = event["content"].strip()

        context = summary or content
        context = context[:1500]

        article = (
            f"Published: {event['published_at']}\n"
            f"Headline: {event['headline']}"
        )

        if context:
            article += f"\nContext: {context}"

        articles.append(article)

    return "\n\n---\n\n".join(articles)


def analyze_cluster(cluster):
    cluster_text = prepare_cluster_text(cluster)

    prompt = f"""
Analyze this news cluster for stock symbol {cluster['symbol']}.

The technical scanner is evaluating a LONG stock trade.

Return valid JSON with exactly these fields:

{{
  "direction": "bullish | bearish | neutral",
  "confidence": 0.0,
  "meaningful_company_specific_catalyst": true,
  "sufficient_news": true,
  "time_horizon": "intraday | several_days | several_weeks | unclear",
  "catalyst": "short description",
  "risks": ["risk"],
  "invalidation_condition": "specific condition",
  "evidence_headlines": ["headline"]
}}

Rules:

1. Determine direction from the complete cluster, not one isolated headline.
2. Generic market articles or articles merely mentioning the symbol are not
   sufficient company-specific news.
3. Conflicting articles should reduce confidence.
4. Analyst ratings alone may support direction but should not automatically
   outweigh earnings misses or other material company events.
5. Use neutral when there is no clear directional catalyst.
6. Do not recommend an order, position size, or option contract.

News cluster:

{cluster_text}
"""

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a financial-news classification component. "
                    "Return JSON only. Risk rules are deterministic and "
                    "cannot be overridden by the analysis."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    )

    return json.loads(response.choices[0].message.content)


def apply_deterministic_gates(analysis):
    direction = analysis["direction"]
    confidence = float(analysis["confidence"])
    meaningful = bool(
        analysis["meaningful_company_specific_catalyst"]
    )
    sufficient = bool(analysis["sufficient_news"])

    if direction == "bearish":
        stock_gate = "reject"
        stock_reason = "bearish_news_opposes_long_trade"
    elif direction == "bullish":
        stock_gate = "pass"
        stock_reason = "bullish_news_supports_long_trade"
    else:
        stock_gate = "pass"
        stock_reason = "neutral_news_does_not_block_technical_stock_trade"

    options_pass = (
        direction == "bullish"
        and confidence >= 0.60
        and meaningful
        and sufficient
    )

    if options_pass:
        options_gate = "pass"
        options_reason = "clear_bullish_company_specific_catalyst"
    else:
        options_gate = "reject"
        options_reason = "options_require_clear_bullish_material_news"

    return {
        "stock_gate": stock_gate,
        "stock_reason": stock_reason,
        "options_gate": options_gate,
        "options_reason": options_reason,
    }


def main():
    symbols = [symbol.upper() for symbol in sys.argv[1:]]
    if not symbols:
        symbols = DEFAULT_SYMBOLS

    news = load_news(symbols)

    if not news:
        raise RuntimeError("No stored Alpaca news found")

    clusters = build_clusters(news)
    latest_clusters = select_latest_cluster_per_symbol(clusters)

    print(f"Stored news events: {len(news)}")
    print(f"Event clusters: {len(clusters)}")
    print(f"Latest clusters selected: {len(latest_clusters)}")

    print("\nAI CLUSTER ANALYSIS")

    for symbol in symbols:
        cluster = latest_clusters.get(symbol)

        if cluster is None:
            print(f"\n{symbol}: no cluster")
            continue

        print(
            f"\n{symbol} | anchor={cluster['anchor']} | "
            f"articles={len(cluster['events'])}"
        )

        analysis = analyze_cluster(cluster)
        gates = apply_deterministic_gates(analysis)

        print(
            f"Direction: {analysis['direction']} | "
            f"confidence={analysis['confidence']}"
        )
        print(
            "Company-specific catalyst: "
            f"{analysis['meaningful_company_specific_catalyst']}"
        )
        print(f"Sufficient news: {analysis['sufficient_news']}")
        print(f"Catalyst: {analysis['catalyst']}")
        print(
            f"STOCK: {gates['stock_gate']} | "
            f"{gates['stock_reason']}"
        )
        print(
            f"OPTIONS: {gates['options_gate']} | "
            f"{gates['options_reason']}"
        )

        print("Evidence:")
        for headline in analysis.get("evidence_headlines", []):
            print(f"  - {headline}")

    print("\nAI NEWS CLUSTER ANALYSIS TEST: OK")


if __name__ == "__main__":
    main()