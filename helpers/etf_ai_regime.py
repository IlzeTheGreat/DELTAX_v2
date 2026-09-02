from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"

DEFAULT_SINCE_HOURS = 24
MIN_EVENT_CONFIDENCE = 0.65
MIN_SOURCE_QUALITY = 0.50
MAX_EVENT_AGE_HOURS = 48

SECTOR_TO_ETF = {
    "technology": "XLK",
    "information technology": "XLK",
    "tech": "XLK",
    "financials": "XLF",
    "financial": "XLF",
    "banks": "XLF",
    "health care": "XLV",
    "healthcare": "XLV",
    "communication services": "XLC",
    "communications": "XLC",
    "communication": "XLC",
    "consumer discretionary": "XLY",
    "consumer cyclicals": "XLY",
    "consumer cyclical": "XLY",
    "consumer staples": "XLP",
    "consumer defensive": "XLP",
    "industrials": "XLI",
    "industrial": "XLI",
    "energy": "XLE",
    "oil & gas": "XLE",
    "oil and gas": "XLE",
    "utilities": "XLU",
    "utility": "XLU",
    "materials": "XLB",
    "basic materials": "XLB",
    "real estate": "XLRE",
    "reits": "XLRE",
}

ETF_NAMES = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Healthcare",
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLI": "Industrials",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "SMH": "Semiconductors",
    "IGV": "Software",
    "CIBR": "Cybersecurity",
    "XBI": "Biotech",
    "IHI": "Medical Devices",
    "KRE": "Regional Banks",
    "IAI": "Broker-Dealers",
    "ITA": "Aerospace & Defense",
    "XOP": "Oil & Gas Exploration",
    "USO": "Crude Oil",
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "IWM": "Russell 2000",
}

# Conservative, causal expansions only.
SUBSECTOR_EXPANSION = {
    "XLK": ["SMH", "IGV", "CIBR"],
    "XLF": ["KRE", "IAI"],
    "XLV": ["XBI", "IHI"],
    "XLI": ["ITA"],
    "XLE": ["XOP", "USO"],
}

REGIME_KEYWORDS = {
    "geopolitical_energy_shock": (
        "iran", "hormuz", "oil", "crude", "tanker", "shipping",
        "missile", "attack", "war", "strait", "supply disruption",
    ),
    "rates_inflation_shock": (
        "inflation", "cpi", "ppi", "interest rate", "rates",
        "fed", "federal reserve", "yield", "treasury",
    ),
    "growth_slowdown": (
        "recession", "slowdown", "unemployment", "payroll",
        "jobs", "gdp", "consumer weakness",
    ),
    "risk_on_growth": (
        "risk-on", "risk on", "growth rally", "soft landing",
        "rate cut", "easing",
    ),
}


def load_env() -> str:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    database_url = (os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL in .env")
    return database_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate DELTAX market AI analyses into ETF regime bias."
    )
    parser.add_argument("--since-hours", type=int, default=DEFAULT_SINCE_HOURS)
    args = parser.parse_args()
    if not 1 <= args.since_hours <= MAX_EVENT_AGE_HOURS:
        parser.error(f"--since-hours must be between 1 and {MAX_EVENT_AGE_HOURS}")
    return args


def canonical_sector(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def signed(direction: str) -> float:
    if direction == "bullish":
        return 1.0
    if direction == "bearish":
        return -1.0
    return 0.0


def recency_weight(published_at: datetime, now: datetime) -> float:
    age_hours = max(
        0.0,
        (now - published_at.astimezone(timezone.utc)).total_seconds() / 3600.0,
    )
    if age_hours <= 6:
        return 1.00
    if age_hours <= 12:
        return 0.85
    if age_hours <= 24:
        return 0.65
    return 0.40


def load_completed_market_ai(database_url: str, since_hours: int) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    cluster_key,
                    first_published_at,
                    last_published_at,
                    analysis_status,
                    analysis_metadata -> 'ai' -> 'result' AS ai_result
                FROM event_clusters
                WHERE scope = 'market'
                  AND event_type = 'market_news'
                  AND analysis_status = 'completed'
                  AND last_published_at >= %s
                  AND analysis_metadata -> 'ai' -> 'result' IS NOT NULL
                ORDER BY last_published_at DESC
                """,
                (cutoff,),
            )
            return [dict(row) for row in cursor.fetchall()]


def event_is_usable(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict) or result.get("market_material") is not True:
        return False
    try:
        market_conf = float(result.get("market_confidence", 0))
        source_quality = float(result.get("source_quality", 0))
    except (TypeError, ValueError):
        return False
    return market_conf >= MIN_EVENT_CONFIDENCE and source_quality >= MIN_SOURCE_QUALITY


def detect_regime_label(events: list[dict[str, Any]]) -> str:
    combined = " ".join(
        str(event.get("event_summary") or "").lower()
        for event in events
    )
    scores = {
        regime: sum(1 for keyword in keywords if keyword in combined)
        for regime, keywords in REGIME_KEYWORDS.items()
    }
    if not scores or max(scores.values()) <= 0:
        return "mixed_or_unclear"
    return max(scores, key=scores.get)


def classify_score(score: float) -> str:
    if score >= 0.25:
        return "long"
    if score <= -0.25:
        return "short"
    return "conflict"


def aggregate(events: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    sector_signed_sum: dict[str, float] = defaultdict(float)
    sector_base_sum: dict[str, float] = defaultdict(float)
    reasons: dict[str, list[str]] = defaultdict(list)

    index_signed_sum: dict[str, float] = defaultdict(float)
    index_base_sum: dict[str, float] = defaultdict(float)

    evidence = []
    used_results = []

    for event in events:
        result = event["ai_result"]
        if not event_is_usable(result):
            continue

        published = event["last_published_at"]
        rw = recency_weight(published, now)
        market_conf = float(result["market_confidence"])
        source_quality = float(result["source_quality"])

        # Base event weight contains event credibility/recency.
        base_weight = rw * market_conf * source_quality
        used_results.append(result)

        evidence.append({
            "cluster_key": event["cluster_key"],
            "last_published_at": published.isoformat(),
            "event_summary": result.get("event_summary"),
            "market_confidence": market_conf,
            "source_quality": source_quality,
            "time_horizon": result.get("time_horizon"),
            "recency_weight": rw,
        })

        for impact in result.get("affected_sectors", []):
            if not isinstance(impact, dict):
                continue

            etf = SECTOR_TO_ETF.get(canonical_sector(impact.get("sector")))
            if not etf:
                continue

            direction = str(impact.get("direction") or "").lower()
            if direction not in {"bullish", "bearish", "neutral"}:
                continue

            try:
                impact_conf = float(impact.get("confidence", 0))
            except (TypeError, ValueError):
                continue

            if impact_conf < MIN_EVENT_CONFIDENCE:
                continue

            # FIX vs v1:
            # denominator is event credibility weight, NOT credibility*impact_conf.
            # Therefore 0.70-confidence bullish evidence produces roughly +0.70,
            # not an artificial +1.00.
            sector_signed_sum[etf] += signed(direction) * impact_conf * base_weight
            sector_base_sum[etf] += base_weight

            reason = str(impact.get("reason") or "").strip()
            if reason and len(reasons[etf]) < 3:
                reasons[etf].append(reason)

        for impact in result.get("index_impacts", []):
            if not isinstance(impact, dict):
                continue

            symbol = str(impact.get("symbol") or "").upper()
            if symbol not in {"SPY", "QQQ", "IWM"}:
                continue

            direction = str(impact.get("direction") or "").lower()
            if direction not in {"bullish", "bearish", "neutral"}:
                continue

            try:
                impact_conf = float(impact.get("confidence", 0))
            except (TypeError, ValueError):
                continue

            if impact_conf < MIN_EVENT_CONFIDENCE:
                continue

            index_signed_sum[symbol] += signed(direction) * impact_conf * base_weight
            index_base_sum[symbol] += base_weight

    etf_biases = []
    for etf, numerator in sector_signed_sum.items():
        denominator = sector_base_sum.get(etf, 0.0)
        score = 0.0 if denominator <= 0 else numerator / denominator
        direction = classify_score(score)
        confidence = min(1.0, abs(score))

        etf_biases.append({
            "symbol": etf,
            "name": ETF_NAMES[etf],
            "direction": direction,
            "score": round(score, 4),
            "confidence": round(confidence, 4),
            "reasons": reasons.get(etf, []),
            "subsector_candidates": SUBSECTOR_EXPANSION.get(etf, []),
        })

    index_biases = []
    for symbol, numerator in index_signed_sum.items():
        denominator = index_base_sum.get(symbol, 0.0)
        score = 0.0 if denominator <= 0 else numerator / denominator
        direction = classify_score(score)
        index_biases.append({
            "symbol": symbol,
            "direction": direction,
            "score": round(score, 4),
            "confidence": round(min(1.0, abs(score)), 4),
        })

    etf_biases.sort(
        key=lambda x: (
            0 if x["direction"] in {"long", "short"} else 1,
            -x["confidence"],
        )
    )
    index_biases.sort(key=lambda x: -x["confidence"])

    long_core = [
        x["symbol"] for x in etf_biases
        if x["direction"] == "long" and x["confidence"] >= MIN_EVENT_CONFIDENCE
    ]
    short_core = [
        x["symbol"] for x in etf_biases
        if x["direction"] == "short" and x["confidence"] >= MIN_EVENT_CONFIDENCE
    ]
    conflicts = [x["symbol"] for x in etf_biases if x["direction"] == "conflict"]

    focused_long = []
    focused_short = []
    for item in etf_biases:
        if item["confidence"] < MIN_EVENT_CONFIDENCE:
            continue
        if item["direction"] == "long":
            focused_long.extend(item["subsector_candidates"])
        elif item["direction"] == "short":
            focused_short.extend(item["subsector_candidates"])

    index_long = [
        x["symbol"] for x in index_biases
        if x["direction"] == "long" and x["confidence"] >= MIN_EVENT_CONFIDENCE
    ]
    index_short = [
        x["symbol"] for x in index_biases
        if x["direction"] == "short" and x["confidence"] >= MIN_EVENT_CONFIDENCE
    ]

    all_conf = (
        [x["confidence"] for x in etf_biases if x["direction"] != "conflict"]
        + [x["confidence"] for x in index_biases if x["direction"] != "conflict"]
    )

    return {
        "generated_at": now.isoformat(),
        "regime": detect_regime_label(used_results),
        "regime_confidence": round(max(all_conf, default=0.0), 4),
        "market_event_count": len(used_results),
        "long_core_etfs": sorted(set(long_core)),
        "short_core_etfs": sorted(set(short_core)),
        "long_focused_candidates": sorted(set(focused_long)),
        "short_focused_candidates": sorted(set(focused_short)),
        "long_index_candidates": sorted(set(index_long)),
        "short_index_candidates": sorted(set(index_short)),
        "avoid_conflicts": sorted(set(conflicts)),
        "etf_biases": etf_biases,
        "index_biases": index_biases,
        "evidence": evidence,
    }


def print_report(result: dict[str, Any]) -> None:
    print("=" * 100)
    print("DELTAX ETF AI REGIME ENGINE v2")
    print("=" * 100)
    print(f"Generated:          {result['generated_at']}")
    print(f"Regime:             {result['regime']}")
    print(f"Regime confidence:  {result['regime_confidence']:.2f}")
    print(f"Usable AI events:   {result['market_event_count']}")
    print()

    sections = [
        ("CORE LONG BIAS", result["long_core_etfs"]),
        ("CORE SHORT BIAS", result["short_core_etfs"]),
        ("FOCUSED LONG CANDIDATES", result["long_focused_candidates"]),
        ("FOCUSED SHORT CANDIDATES", result["short_focused_candidates"]),
        ("INDEX LONG CANDIDATES", result["long_index_candidates"]),
        ("INDEX SHORT CANDIDATES", result["short_index_candidates"]),
        ("AVOID / CONFLICT", result["avoid_conflicts"]),
    ]
    for title, items in sections:
        print(title)
        print(", ".join(items) if items else "None")
        print()

    print("ETF DETAILS")
    print("-" * 100)
    for item in result["etf_biases"]:
        reason = item["reasons"][0] if item["reasons"] else ""
        print(
            f"{item['symbol']:<5} {item['direction'].upper():<9} "
            f"confidence={item['confidence']:.2f} "
            f"score={item['score']:+.2f} {item['name']}"
        )
        if reason:
            print(f"      {reason}")

    print()
    print("INDEX BIAS")
    for item in result["index_biases"]:
        print(
            f"{item['symbol']}: {item['direction'].upper()} "
            f"confidence={item['confidence']:.2f} score={item['score']:+.2f}"
        )

    print()
    print("JSON_RESULT")
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def main() -> int:
    args = parse_args()
    database_url = load_env()
    events = load_completed_market_ai(database_url, args.since_hours)
    result = aggregate(events)
    print_report(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
