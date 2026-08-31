# File: deltax/news_ai_processor.py
# Purpose: Classifies unprocessed production news clusters with a direction-neutral OpenAI prompt and persists only AI facts, never deterministic trade gates.

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

PROMPT_VERSION = "deltax_news_cluster_v2"
DEFAULT_SINCE_HOURS = 72
DEFAULT_LIMIT = 10


class NewsAIProcessor:
    def __init__(self, database_url=DATABASE_URL):
        self.database_url = database_url
        self.client = OpenAI(api_key=OPENAI_API_KEY)

    def load_clusters(self, since_hours):
        published_after = datetime.now(timezone.utc) - timedelta(
            hours=since_hours
        )

        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        clusters.id AS event_cluster_id,
                        clusters.cluster_key,
                        clusters.primary_symbol AS symbol,
                        clusters.first_published_at,
                        clusters.last_published_at,
                        events.id AS source_event_id,
                        events.external_id,
                        events.headline,
                        COALESCE(events.summary, '') AS summary,
                        COALESCE(events.content, '') AS content,
                        events.published_at,
                        events.ingested_at
                    FROM event_clusters clusters
                    JOIN event_cluster_members members
                        ON members.event_cluster_id = clusters.id
                    JOIN source_events events
                        ON events.id = members.source_event_id
                    WHERE clusters.event_type = 'news'
                      AND clusters.last_published_at >= %s
                    ORDER BY
                        clusters.last_published_at DESC,
                        clusters.id,
                        events.published_at,
                        events.id
                    """,
                    (published_after,),
                )
                rows = cursor.fetchall()

        clusters = {}

        for row in rows:
            cluster_id = row["event_cluster_id"]

            if cluster_id not in clusters:
                clusters[cluster_id] = {
                    "id": cluster_id,
                    "cluster_key": row["cluster_key"],
                    "symbol": row["symbol"],
                    "first_published_at": row[
                        "first_published_at"
                    ],
                    "last_published_at": row[
                        "last_published_at"
                    ],
                    "events": [],
                }

            clusters[cluster_id]["events"].append(
                {
                    "id": row["source_event_id"],
                    "external_id": row["external_id"],
                    "headline": row["headline"] or "",
                    "summary": row["summary"],
                    "content": row["content"],
                    "published_at": row["published_at"],
                    "ingested_at": row["ingested_at"],
                }
            )

        return sorted(
            clusters.values(),
            key=lambda item: item["last_published_at"],
            reverse=True,
        )

    @staticmethod
    def calculate_input_hash(cluster):
        payload = {
            "cluster_key": cluster["cluster_key"],
            "events": [
                {
                    "id": str(event["id"]),
                    "external_id": event["external_id"],
                    "headline": event["headline"],
                    "summary": event["summary"],
                    "content": event["content"],
                    "published_at": event[
                        "published_at"
                    ].isoformat(),
                }
                for event in sorted(
                    cluster["events"],
                    key=lambda item: (
                        item["published_at"],
                        str(item["id"]),
                    ),
                )
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        return hashlib.sha256(encoded).hexdigest()

    def find_existing(self, cluster, input_hash):
        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        id,
                        status,
                        completed_at
                    FROM ai_analyses
                    WHERE event_cluster_id = %s
                      AND symbol = %s
                      AND model = %s
                      AND prompt_version = %s
                      AND input_hash = %s
                    LIMIT 1
                    """,
                    (
                        cluster["id"],
                        cluster["symbol"],
                        OPENAI_MODEL,
                        PROMPT_VERSION,
                        input_hash,
                    ),
                )
                return cursor.fetchone()

    def pending_clusters(self, since_hours):
        clusters = self.load_clusters(since_hours)
        pending = []
        completed = []

        for cluster in clusters:
            input_hash = self.calculate_input_hash(cluster)
            existing = self.find_existing(cluster, input_hash)
            item = {
                **cluster,
                "input_hash": input_hash,
                "existing": existing,
            }

            if existing and existing["status"] == "completed":
                completed.append(item)
            else:
                pending.append(item)

        return pending, completed

    @staticmethod
    def prepare_cluster_text(cluster):
        articles = []

        for event in cluster["events"]:
            context = (
                event["summary"].strip()
                or event["content"].strip()
            )[:1800]
            article = (
                f"Published: {event['published_at'].isoformat()}\n"
                f"Headline: {event['headline']}"
            )

            if context:
                article += f"\nContext: {context}"

            articles.append(article)

        return "\n\n---\n\n".join(articles)

    def analyze_cluster(self, cluster):
        cluster_text = self.prepare_cluster_text(cluster)
        prompt = f"""
Classify the likely directional market impact of this complete news cluster
for stock symbol {cluster['symbol']}.

Return valid JSON with exactly these fields:

{{
  "direction": "bullish | bearish | neutral",
  "confidence": 0.0,
  "meaningful_company_specific_catalyst": true,
  "sufficient_news": true,
  "time_horizon": "intraday | several_days | several_weeks | unclear",
  "catalyst": "short factual description",
  "risks": ["risk"],
  "invalidation_condition": "specific factual condition",
  "evidence_headlines": ["headline"]
}}

Rules:

1. Assess direction without assuming a long or short trade.
2. Analyze the complete cluster, not one isolated headline.
3. Set meaningful_company_specific_catalyst=false for generic market stories,
   list articles, passive mentions, or retrospective commentary.
4. Set sufficient_news=false when the available text does not establish what
   happened or why it could affect this company.
5. Conflicting evidence must reduce confidence; use neutral when there is no
   clear directional catalyst.
6. Confidence is directional confidence, not confidence in reading the text.
7. Do not recommend an order, position size, strategy, or option contract.
8. Do not apply technical, risk, price-confirmation, or execution gates.

News cluster:

{cluster_text}
"""
        response = self.client.chat.completions.create(
            model=OPENAI_MODEL,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a financial-news classification component. "
                        "Return JSON only. Deterministic trading and risk "
                        "decisions are outside your responsibility."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        )
        analysis = json.loads(response.choices[0].message.content)
        self.validate_analysis(analysis)
        return analysis

    @staticmethod
    def validate_analysis(analysis):
        required = {
            "direction",
            "confidence",
            "meaningful_company_specific_catalyst",
            "sufficient_news",
            "time_horizon",
            "catalyst",
            "risks",
            "invalidation_condition",
            "evidence_headlines",
        }
        missing = sorted(required - set(analysis))

        if missing:
            raise ValueError(
                "AI response missing fields: " + ", ".join(missing)
            )

        if analysis["direction"] not in {
            "bullish",
            "bearish",
            "neutral",
        }:
            raise ValueError("Invalid AI direction")

        confidence = float(analysis["confidence"])

        if not 0 <= confidence <= 1:
            raise ValueError("AI confidence must be between 0 and 1")

        if not isinstance(
            analysis["meaningful_company_specific_catalyst"],
            bool,
        ):
            raise ValueError("meaningful catalyst must be boolean")

        if not isinstance(analysis["sufficient_news"], bool):
            raise ValueError("sufficient_news must be boolean")

        if analysis["time_horizon"] not in {
            "intraday",
            "several_days",
            "several_weeks",
            "unclear",
        }:
            raise ValueError("Invalid AI time horizon")

        if not isinstance(analysis["risks"], list):
            raise ValueError("AI risks must be a list")

        if not isinstance(analysis["evidence_headlines"], list):
            raise ValueError("AI evidence_headlines must be a list")

    @staticmethod
    def map_time_horizon(value):
        return {
            "intraday": "intraday",
            "several_days": "active",
            "several_weeks": "core",
            "unclear": "unknown",
        }[value]

    @staticmethod
    def impact_score(direction, confidence):
        score = round(float(confidence) * 100)

        if direction == "bullish":
            return score

        if direction == "bearish":
            return -score

        return 0

    @staticmethod
    def trade_relevance(analysis):
        confidence = float(analysis["confidence"])

        if (
            analysis["meaningful_company_specific_catalyst"]
            and analysis["sufficient_news"]
        ):
            return confidence

        return min(confidence, 0.30)

    def reserve_analysis(self, cluster):
        with psycopg.connect(
            self.database_url,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ai_analyses (
                        event_cluster_id,
                        symbol,
                        model,
                        prompt_version,
                        input_hash,
                        status,
                        event_type
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        'running',
                        'news_cluster'
                    )
                    ON CONFLICT (
                        event_cluster_id,
                        symbol,
                        model,
                        prompt_version,
                        input_hash
                    )
                    WHERE event_cluster_id IS NOT NULL
                    DO UPDATE SET
                        status = 'running',
                        error_message = NULL,
                        requested_at = now(),
                        completed_at = NULL
                    WHERE ai_analyses.status IN (
                        'failed',
                        'refused'
                    )
                    RETURNING id
                    """,
                    (
                        cluster["id"],
                        cluster["symbol"],
                        OPENAI_MODEL,
                        PROMPT_VERSION,
                        cluster["input_hash"],
                    ),
                )
                row = cursor.fetchone()
                connection.commit()

        return row["id"] if row else None

    def complete_analysis(self, analysis_id, analysis):
        confidence = float(analysis["confidence"])
        direction = analysis["direction"]

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE ai_analyses
                    SET
                        status = 'completed',
                        direction = %s,
                        impact_score = %s,
                        confidence = %s,
                        time_horizon = %s,
                        trade_relevance = %s,
                        catalyst = %s,
                        facts = %s,
                        risks = %s,
                        invalidation_condition = %s,
                        raw_response = %s,
                        error_message = NULL,
                        completed_at = now()
                    WHERE id = %s
                      AND status = 'running'
                    """,
                    (
                        direction,
                        self.impact_score(direction, confidence),
                        confidence,
                        self.map_time_horizon(
                            analysis["time_horizon"]
                        ),
                        self.trade_relevance(analysis),
                        analysis["catalyst"],
                        Jsonb(analysis["evidence_headlines"]),
                        Jsonb(analysis["risks"]),
                        analysis["invalidation_condition"],
                        Jsonb(analysis),
                        analysis_id,
                    ),
                )

                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Could not complete AI analysis {analysis_id}"
                    )

                connection.commit()

    def fail_analysis(self, analysis_id, error):
        message = str(error)[:2000]

        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE ai_analyses
                    SET
                        status = 'failed',
                        error_message = %s,
                        completed_at = now()
                    WHERE id = %s
                    """,
                    (message, analysis_id),
                )
                connection.commit()

    def check(self, since_hours):
        pending, completed = self.pending_clusters(since_hours)

        return {
            "model": OPENAI_MODEL,
            "prompt_version": PROMPT_VERSION,
            "since_hours": since_hours,
            "clusters_in_window": len(pending) + len(completed),
            "pending_clusters": len(pending),
            "already_completed": len(completed),
            "next_pending": [
                {
                    "symbol": cluster["symbol"],
                    "cluster_key": cluster["cluster_key"],
                    "last_published_at": cluster[
                        "last_published_at"
                    ],
                    "articles": len(cluster["events"]),
                }
                for cluster in pending[:5]
            ],
            "direction_neutral_prompt": True,
            "deterministic_gates_persisted": False,
            "writes_performed": False,
            "openai_requests_performed": False,
        }

    def process(self, since_hours, limit):
        pending, _ = self.pending_clusters(since_hours)
        selected = pending[:limit]
        results = []

        for cluster in selected:
            analysis_id = self.reserve_analysis(cluster)

            if analysis_id is None:
                results.append(
                    {
                        "symbol": cluster["symbol"],
                        "cluster_key": cluster["cluster_key"],
                        "status": "skipped_locked_or_completed",
                    }
                )
                continue

            try:
                analysis = self.analyze_cluster(cluster)
                self.complete_analysis(analysis_id, analysis)
                results.append(
                    {
                        "symbol": cluster["symbol"],
                        "cluster_key": cluster["cluster_key"],
                        "status": "completed",
                        "direction": analysis["direction"],
                        "confidence": analysis["confidence"],
                        "meaningful": analysis[
                            "meaningful_company_specific_catalyst"
                        ],
                        "sufficient": analysis["sufficient_news"],
                    }
                )
            except Exception as error:
                self.fail_analysis(analysis_id, error)
                results.append(
                    {
                        "symbol": cluster["symbol"],
                        "cluster_key": cluster["cluster_key"],
                        "status": "failed",
                        "error": str(error),
                    }
                )

        return {
            "model": OPENAI_MODEL,
            "prompt_version": PROMPT_VERSION,
            "selected": len(selected),
            "results": results,
        }


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    return str(value)


def parse_args():
    parser = argparse.ArgumentParser(
        description="DELTAX production clustered-news AI processor."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="List pending clusters without writes or OpenAI requests.",
    )
    mode.add_argument(
        "--process",
        action="store_true",
        help="Analyze and persist pending clusters.",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=DEFAULT_SINCE_HOURS,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
    )
    args = parser.parse_args()

    if args.since_hours <= 0:
        parser.error("--since-hours must be greater than zero")

    if args.limit <= 0:
        parser.error("--limit must be greater than zero")

    return args


def main():
    args = parse_args()
    processor = NewsAIProcessor()
    result = (
        processor.check(args.since_hours)
        if args.check
        else processor.process(args.since_hours, args.limit)
    )
    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=json_default,
        )
    )
    print("NEWS AI PROCESSOR: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
