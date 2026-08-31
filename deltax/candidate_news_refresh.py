# File: deltax/candidate_news_refresh.py
# Purpose: Refresh company news ONLY for current S&P 500 technical candidates,
# then run AI ONLY for pending clusters belonging to those candidates.
#
# This module creates no trade intents and submits no broker orders.
# It intentionally re-runs the technical scanner; scan_cycle.py will scan again
# immediately afterwards. That small duplication keeps the existing decision
# pipeline unchanged and safe for the hackathon deadline.

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

try:
    from deltax.company_news_ingestion import CompanyNewsIngestion
    from deltax.news_ai_processor import (
        NewsAIProcessor,
        OPENAI_MODEL,
        PROMPT_VERSION,
    )
    from deltax.technical_scanner import TechnicalScanner
except ModuleNotFoundError:
    # Supports direct execution:
    #   python deltax/candidate_news_refresh.py --check
    from company_news_ingestion import CompanyNewsIngestion
    from news_ai_processor import (
        NewsAIProcessor,
        OPENAI_MODEL,
        PROMPT_VERSION,
    )
    from technical_scanner import TechnicalScanner


UTC = timezone.utc
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_AI_SINCE_HOURS = 72
DEFAULT_AI_LIMIT = 5


def process_targeted_ai(symbols: list[str], since_hours: int, limit: int):
    processor = NewsAIProcessor()
    pending, _ = processor.pending_clusters(since_hours)

    symbol_set = set(symbols)
    targeted = [
        cluster
        for cluster in pending
        if cluster["symbol"] in symbol_set
    ]
    selected = targeted[:limit]
    results = []

    for cluster in selected:
        analysis_id = processor.reserve_analysis(cluster)

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
            analysis = processor.analyze_cluster(cluster)
            processor.complete_analysis(analysis_id, analysis)
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
            processor.fail_analysis(analysis_id, error)
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
        "candidate_symbols": symbols,
        "pending_candidate_clusters": len(targeted),
        "selected": len(selected),
        "remaining_candidate_clusters_after_selection": max(
            0, len(targeted) - len(selected)
        ),
        "results": results,
    }


def refresh_candidate_news(
    symbols: list[str],
    *,
    lookback_hours: int,
):
    ingestion = CompanyNewsIngestion()

    with psycopg.connect(
        ingestion.database_url,
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            ingestion.validate_schema(cursor)
            config = ingestion.active_config(cursor)
            source_type = ingestion.detected_source_type(cursor)

        events, fetch_meta = ingestion.fetch_all(
            symbols,
            lookback_hours,
            batch_size=min(35, max(1, len(symbols))),
            max_pages_per_batch=10,
        )

        valid_symbols = set(symbols)

        with connection.cursor() as cursor:
            event_result = ingestion.persist_events(
                cursor,
                events,
                source_type,
                valid_symbols,
            )

        cutoff = datetime.now(UTC) - timedelta(hours=lookback_hours)

        with connection.cursor() as cursor:
            stored_news = ingestion.load_stored_news(
                cursor,
                symbols,
                cutoff,
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
            sessions = ingestion.fetch_calendar(
                oldest.date() - timedelta(days=3),
                newest.date() + timedelta(days=10),
            )
            clusters = ingestion.create_clusters(
                stored_news,
                sessions,
            )
        else:
            clusters = []

        with connection.cursor() as cursor:
            cluster_result = ingestion.persist_clusters(
                cursor,
                clusters,
                datetime.now(UTC),
            )

        connection.commit()

    return {
        "config_version": config["version"],
        "symbols_requested": symbols,
        "symbol_count": len(symbols),
        "lookback_hours": lookback_hours,
        "fetched_unique_articles": len(events),
        "fetch": fetch_meta,
        "stored_news_in_window": len(stored_news),
        "calculated_clusters": len(clusters),
        "event_persistence": event_result,
        "cluster_persistence": cluster_result,
        "database_writes_performed": True,
    }


def check():
    scanner = TechnicalScanner()
    scanner_health = scanner.health_check()

    ingestion = CompanyNewsIngestion()
    ingestion_health = ingestion.health_check()

    return {
        "status": "ok",
        "scanner": scanner_health,
        "company_news_base_worker": {
            "universe": ingestion_health["universe"],
            "universe_size": ingestion_health["universe_size"],
        },
        "candidate_refresh_design": {
            "technical_universe_expected": "sp500_scan",
            "candidate_news_scope": "technical_candidates_only",
            "targeted_ai_only": True,
            "trade_intents_created": False,
            "broker_orders_submitted": False,
        },
    }


def run(lookback_hours: int, ai_since_hours: int, ai_limit: int):
    scanner = TechnicalScanner()
    scan = scanner.scan()

    if scan["status"] == "skipped":
        return {
            "status": "skipped",
            "reason": scan.get("reason"),
            "session": scan.get("session"),
            "technical_universe_size": scan.get("universe_size"),
            "trade_intents_created": False,
            "broker_orders_submitted": False,
        }

    candidates = scan.get("candidates", [])
    symbols = sorted(
        {
            candidate["symbol"]
            for candidate in candidates
        }
    )

    if not symbols:
        return {
            "status": "completed",
            "technical_universe_size": scan.get("universe_size"),
            "technical_candidates": 0,
            "candidate_symbols": [],
            "news_refresh": {
                "skipped": True,
                "reason": "no_technical_candidates",
            },
            "ai": {
                "selected": 0,
                "results": [],
            },
            "trade_intents_created": False,
            "broker_orders_submitted": False,
        }

    news_result = refresh_candidate_news(
        symbols,
        lookback_hours=lookback_hours,
    )

    ai_result = process_targeted_ai(
        symbols,
        since_hours=ai_since_hours,
        limit=ai_limit,
    )

    return {
        "status": "completed",
        "technical_universe_size": scan.get("universe_size"),
        "technical_candidates": len(candidates),
        "candidate_symbol_count": len(symbols),
        "candidate_symbols": symbols,
        "news_refresh": news_result,
        "ai": ai_result,
        "trade_intents_created": False,
        "broker_orders_submitted": False,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="DELTAX candidate-only company-news refresh."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=DEFAULT_LOOKBACK_HOURS,
    )
    parser.add_argument(
        "--ai-since-hours",
        type=int,
        default=DEFAULT_AI_SINCE_HOURS,
    )
    parser.add_argument(
        "--ai-limit",
        type=int,
        default=DEFAULT_AI_LIMIT,
    )
    return parser.parse_args()


def main():
    args = parse_args()
    result = (
        check()
        if args.check
        else run(
            args.lookback_hours,
            args.ai_since_hours,
            args.ai_limit,
        )
    )
    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    print("CANDIDATE NEWS REFRESH: OK")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
