# File: helpers/test_signal_gates.py
# Purpose: Combines technical candidates, AI news direction, separate stock/options gates, and the selected 10-minute price-confirmation rule.

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv
from openai import OpenAI


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

MARKET_TIMEZONE = ZoneInfo("America/New_York")
ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

TECHNICAL_SIGNALS = [
    {"symbol": "IREN", "strategy": "core", "direction": "long"},
    {"symbol": "PCG", "strategy": "core", "direction": "long"},
    {"symbol": "RKLB", "strategy": "core", "direction": "long"},
    {"symbol": "IREN", "strategy": "active", "direction": "long"},
    {"symbol": "RKLB", "strategy": "active", "direction": "long"},
    {"symbol": "CW", "strategy": "active", "direction": "long"},
]


def fetch_news(symbols: list[str]) -> dict[str, list[dict]]:
    start = datetime.now(timezone.utc) - timedelta(days=7)

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
            "limit": 50,
            "sort": "desc",
            "include_content": "false",
            "exclude_contentless": "true",
        },
        timeout=30,
    )

    response.raise_for_status()
    articles = response.json().get("news", [])

    grouped = {symbol: [] for symbol in symbols}

    for article in articles:
        compact_article = {
            "headline": article.get("headline"),
            "summary": article.get("summary"),
            "created_at": article.get("created_at"),
            "source": article.get("source"),
            "url": article.get("url"),
        }

        for symbol in symbols:
            if symbol in article.get("symbols", []):
                grouped[symbol].append(compact_article)

    return grouped


def analyze_news(
    grouped_news: dict[str, list[dict]],
) -> dict[str, dict]:
    prompt = f"""
You are the AI news-analysis component of the DELTAX paper-trading agent.

Analyze only the supplied news. Do not invent facts.

For every symbol return:

- direction: bullish, bearish, or neutral
- confidence: number from 0.0 to 1.0
- time_horizon: intraday, several_days, several_weeks, or unclear
- catalyst
- risks
- invalidation_condition
- sufficient_news
- evidence_headlines

Important:

1. Neutral means the news does not provide a strong directional conclusion.
2. Neutral does not automatically reject a deterministic stock trade.
3. Options require meaningful news and a clear bullish or bearish direction.
4. If there is no meaningful catalyst, use neutral and sufficient_news false.
5. Output only valid JSON using this structure:

{{
  "analyses": [
    {{
      "symbol": "SYMBOL",
      "direction": "neutral",
      "confidence": 0.2,
      "time_horizon": "unclear",
      "catalyst": "Explanation",
      "risks": ["Risk"],
      "invalidation_condition": "Condition",
      "sufficient_news": false,
      "evidence_headlines": ["Headline"]
    }}
  ]
}}

NEWS:

{json.dumps(grouped_news, ensure_ascii=False, indent=2)}
""".strip()

    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"]
    )

    response = client.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
    )

    text = response.output_text.strip()

    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]

    if text.endswith("```"):
        text = text[:-3]

    result = json.loads(text.strip())

    analyses = {
        item["symbol"]: item
        for item in result["analyses"]
    }

    return analyses


def load_confirmation_bars(
    symbols: list[str],
) -> tuple[dict[str, dict], object]:
    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY_PAPER"],
        os.environ["ALPACA_API_SECRET_PAPER"],
    )

    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=datetime.now(timezone.utc) - timedelta(days=7),
        feed=DataFeed.IEX,
    )

    frame = client.get_stock_bars(request).df.reset_index()

    if frame.empty:
        raise RuntimeError("No confirmation bars returned")

    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"],
        utc=True,
    )

    local_timestamp = frame["timestamp"].dt.tz_convert(
        MARKET_TIMEZONE
    )

    frame["session_date"] = local_timestamp.dt.date
    frame["market_minutes"] = (
        local_timestamp.dt.hour * 60
        + local_timestamp.dt.minute
    )

    frame = frame[
        (frame["market_minutes"] >= 570)
        & (frame["market_minutes"] < 960)
    ].copy()

    latest_session = frame["session_date"].max()
    confirmation = {}

    for symbol in symbols:
        symbol_frame = frame[
            (frame["symbol"] == symbol)
            & (frame["session_date"] == latest_session)
        ].sort_values("timestamp")

        if len(symbol_frame) < 3:
            raise RuntimeError(
                f"Not enough 5-minute bars for {symbol}"
            )

        # Bar -3 represents the price 10 minutes before the latest bar.
        signal_bar = symbol_frame.iloc[-3]
        confirmation_bar = symbol_frame.iloc[-1]

        confirmation[symbol] = {
            "signal_price": float(signal_bar["close"]),
            "confirmation_price": float(
                confirmation_bar["close"]
            ),
            "signal_time": signal_bar["timestamp"],
            "confirmation_time": confirmation_bar["timestamp"],
        }

    return confirmation, latest_session


def apply_gates(
    signal: dict,
    analysis: dict,
    price_data: dict,
) -> dict:
    direction = signal["direction"]
    ai_direction = analysis["direction"]

    required_ai_direction = (
        "bullish"
        if direction == "long"
        else "bearish"
    )

    opposite_ai_direction = (
        "bearish"
        if direction == "long"
        else "bullish"
    )

    # Neutral news may allow a deterministic stock trade.
    stock_ai_pass = (
        ai_direction != opposite_ai_direction
    )

    # Options require clear aligned direction and meaningful news.
    options_ai_pass = (
        ai_direction == required_ai_direction
        and analysis["sufficient_news"] is True
        and signal["strategy"] in {"core", "active"}
    )

    if direction == "long":
        price_confirmed = (
            price_data["confirmation_price"]
            > price_data["signal_price"]
        )
    else:
        price_confirmed = (
            price_data["confirmation_price"]
            < price_data["signal_price"]
        )

    return {
        **signal,
        "ai_direction": ai_direction,
        "confidence": analysis["confidence"],
        "sufficient_news": analysis["sufficient_news"],
        "signal_price": price_data["signal_price"],
        "confirmation_price": price_data[
            "confirmation_price"
        ],
        "price_confirmed": price_confirmed,
        "stock_ai_pass": stock_ai_pass,
        "options_ai_pass": options_ai_pass,
        "stock_gate": (
            "pass"
            if stock_ai_pass and price_confirmed
            else "reject"
        ),
        "options_gate": (
            "pass"
            if options_ai_pass and price_confirmed
            else "reject"
        ),
    }


if __name__ == "__main__":
    symbols = sorted(
        {
            signal["symbol"]
            for signal in TECHNICAL_SIGNALS
        }
    )

    print("Symbols: " + ", ".join(symbols))

    grouped_news = fetch_news(symbols)

    for symbol in symbols:
        print(
            f"{symbol}: "
            f"{len(grouped_news[symbol])} news articles"
        )

    analyses = analyze_news(grouped_news)

    missing_analyses = set(symbols) - set(analyses)

    if missing_analyses:
        raise RuntimeError(
            "Missing AI analysis for: "
            + ", ".join(sorted(missing_analyses))
        )

    confirmation, session_date = load_confirmation_bars(
        symbols
    )

    print(f"\nConfirmation session: {session_date}")
    print("Confirmation rule: A — net movement after 10 minutes")

    results = []

    for signal in TECHNICAL_SIGNALS:
        symbol = signal["symbol"]

        result = apply_gates(
            signal=signal,
            analysis=analyses[symbol],
            price_data=confirmation[symbol],
        )

        results.append(result)

    print("\nFINAL GATES")

    for result in results:
        print(
            f"{result['symbol']} "
            f"{result['strategy'].upper()}: "
            f"AI={result['ai_direction']}, "
            f"confidence={result['confidence']:.2f}, "
            f"price {result['signal_price']:.2f}"
            f" -> {result['confirmation_price']:.2f}, "
            f"confirmed={result['price_confirmed']}, "
            f"STOCK={result['stock_gate']}, "
            f"OPTIONS={result['options_gate']}"
        )

    print("\nSIGNAL GATE TEST: OK")