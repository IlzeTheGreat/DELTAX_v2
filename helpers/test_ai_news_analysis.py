# File: helpers/test_ai_news_analysis.py
# Purpose: Fetches recent Alpaca news for technical candidates and asks OpenAI to return the required directional trading analysis.

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

CANDIDATE_SYMBOLS = ["IREN", "PCG", "RKLB", "CW"]
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
NEWS_LOOKBACK_DAYS = 7
MAX_NEWS_ITEMS = 50


def fetch_news(symbols: list[str]) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(
        days=NEWS_LOOKBACK_DAYS
    )

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
        params={
            "symbols": ",".join(symbols),
            "start": start.isoformat(),
            "limit": MAX_NEWS_ITEMS,
            "sort": "desc",
            "include_content": "false",
            "exclude_contentless": "true",
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json().get("news", [])


def group_news_by_symbol(
    symbols: list[str],
    articles: list[dict],
) -> dict[str, list[dict]]:
    grouped = {symbol: [] for symbol in symbols}

    for article in articles:
        article_symbols = article.get("symbols", [])

        compact_article = {
            "headline": article.get("headline"),
            "summary": article.get("summary"),
            "created_at": article.get("created_at"),
            "source": article.get("source"),
            "url": article.get("url"),
        }

        for symbol in symbols:
            if symbol in article_symbols:
                grouped[symbol].append(compact_article)

    return grouped


def build_prompt(
    grouped_news: dict[str, list[dict]],
) -> str:
    payload = json.dumps(
        grouped_news,
        ensure_ascii=False,
        indent=2,
    )

    return f"""
You are the AI analysis gate for DELTAX, an Alpaca paper-trading agent. You are a stock market expert.

Analyze only the supplied news. Do not invent facts or use unstated
information.

For every supplied stock symbol, return:

- direction: bullish, bearish, or neutral
- confidence: number from 0.0 to 1.0
- time_horizon: intraday, several_days, several_weeks, or unclear
- catalyst: concise explanation of the relevant catalyst
- risks: array of concise risks
- invalidation_condition: a concrete condition that would invalidate
  the directional conclusion
- recommended_strategy: core, active, intraday, or none
- options_eligible: true only when recommended_strategy is core or active
- sufficient_news: whether the supplied news is sufficient for a conclusion
- evidence_headlines: array containing only headlines from the supplied data

Rules:

1. If there is no meaningful recent catalyst, direction must be neutral.
2. Neutral conclusions must use recommended_strategy "none".
3. Do not approve a trade based only on price movement.
4. Negative or conflicting evidence must reduce confidence.
5. Output only valid JSON.
6. Use exactly this structure:

{{
  "analyses": [
    {{
      "symbol": "SYMBOL",
      "direction": "bullish",
      "confidence": 0.75,
      "time_horizon": "several_days",
      "catalyst": "Explanation",
      "risks": ["Risk"],
      "invalidation_condition": "Condition",
      "recommended_strategy": "active",
      "options_eligible": true,
      "sufficient_news": true,
      "evidence_headlines": ["Headline"]
    }}
  ]
}}

NEWS DATA:

{payload}
""".strip()


def parse_json_response(response_text: str) -> dict:
    cleaned = response_text.strip()

    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]

    if cleaned.startswith("```"):
        cleaned = cleaned[3:]

    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]

    return json.loads(cleaned.strip())


def analyze_news(
    grouped_news: dict[str, list[dict]],
) -> dict:
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"]
    )

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=build_prompt(grouped_news),
    )

    return parse_json_response(response.output_text)


def validate_analysis(
    result: dict,
    expected_symbols: list[str],
) -> None:
    analyses = result.get("analyses")

    if not isinstance(analyses, list):
        raise RuntimeError(
            "OpenAI response does not contain an analyses list"
        )

    returned_symbols = {
        item.get("symbol")
        for item in analyses
    }

    missing_symbols = (
        set(expected_symbols) - returned_symbols
    )

    if missing_symbols:
        raise RuntimeError(
            "Missing AI analyses for: "
            + ", ".join(sorted(missing_symbols))
        )

    allowed_directions = {
        "bullish",
        "bearish",
        "neutral",
    }
    allowed_strategies = {
        "core",
        "active",
        "intraday",
        "none",
    }

    for item in analyses:
        if item.get("direction") not in allowed_directions:
            raise RuntimeError(
                f"Invalid direction for {item.get('symbol')}"
            )

        if (
            item.get("recommended_strategy")
            not in allowed_strategies
        ):
            raise RuntimeError(
                f"Invalid strategy for {item.get('symbol')}"
            )

        confidence = item.get("confidence")

        if (
            not isinstance(confidence, (int, float))
            or confidence < 0
            or confidence > 1
        ):
            raise RuntimeError(
                f"Invalid confidence for {item.get('symbol')}"
            )


if __name__ == "__main__":
    print(
        "Technical candidates: "
        + ", ".join(CANDIDATE_SYMBOLS)
    )

    articles = fetch_news(CANDIDATE_SYMBOLS)

    print(f"Alpaca news articles received: {len(articles)}")

    grouped_news = group_news_by_symbol(
        CANDIDATE_SYMBOLS,
        articles,
    )

    for symbol in CANDIDATE_SYMBOLS:
        print(
            f"{symbol}: "
            f"{len(grouped_news[symbol])} articles"
        )

    result = analyze_news(grouped_news)

    validate_analysis(
        result=result,
        expected_symbols=CANDIDATE_SYMBOLS,
    )

    print("\nAI NEWS ANALYSIS")
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )

    print("\nAI NEWS ANALYSIS TEST: OK")